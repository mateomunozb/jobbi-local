"""Contexto delimitado: Comunicación.

Dueño de la mensajería (Conversación, Mensaje) y de las notificaciones, en la
base `jobbi_comunicacion`. Las conversaciones cuelgan de un Contacto del
contexto Mercado, referenciado solo por su UUID.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from common.db import condiciones, inicializar, nuevo_id, paginar_consulta
from common.service import crear_servicio

from .models import Conversacion, Mensaje, Notificacion
from .tablas import ConversacionFila, MensajeFila, NotificacionFila

app = crear_servicio(
    nombre="comunicacion",
    contexto="Comunicación",
    descripcion=(
        "Conversaciones y mensajes entre demandante y prestador, y las "
        "notificaciones enviadas a los usuarios por sus distintos canales."
    ),
)


Sesion: sessionmaker[Session] = inicializar("comunicacion")


def sesion() -> Session:
    with Sesion() as s:
        yield s


# --- Escrituras ------------------------------------------------------------

class AltaConversacion(BaseModel):
    contactoId: str


class AltaMensaje(BaseModel):
    conversacionId: str
    remitenteId: str
    contenido: str = Field(min_length=1, max_length=2000)


class AltaNotificacion(BaseModel):
    usuarioId: str
    tipo: str = Field(max_length=40)
    canal: str = Field(default="PUSH", max_length=20)
    contenido: str = Field(min_length=1, max_length=500)


@app.post("/conversaciones", tags=["mensajería"], status_code=201,
          summary="Abrir la conversación de un contacto")
def alta_conversacion(peticion: AltaConversacion, s: Session = Depends(sesion)):
    # Un contacto tiene a lo sumo una conversación (0..1 en el modelo de
    # dominio), así que volver a abrirla devuelve la misma en lugar de duplicar.
    existente = s.scalars(select(ConversacionFila).where(
        ConversacionFila.contactoId == peticion.contactoId
    )).first()
    if existente:
        return Conversacion.model_validate(existente)

    conversacion = ConversacionFila(
        id=nuevo_id(), contactoId=peticion.contactoId, fechaInicio=date.today(),
    )
    s.add(conversacion)
    s.commit()
    return Conversacion.model_validate(conversacion)


@app.post("/mensajes", tags=["mensajería"], status_code=201, summary="Enviar un mensaje")
def alta_mensaje(peticion: AltaMensaje, s: Session = Depends(sesion)):
    if s.get(ConversacionFila, peticion.conversacionId) is None:
        raise HTTPException(404, f"Conversacion '{peticion.conversacionId}' no encontrada")

    mensaje = MensajeFila(
        id=nuevo_id(),
        conversacionId=peticion.conversacionId,
        remitenteId=peticion.remitenteId,
        contenido=peticion.contenido.strip(),
        fechaEnvio=datetime.now(),
        # Lo escribe quien lo envía, así que nace sin leer para el destinatario.
        leido=False,
    )
    s.add(mensaje)
    s.commit()
    return Mensaje.model_validate(mensaje)


@app.post("/notificaciones", tags=["notificaciones"], status_code=201,
          summary="Registrar una notificación para un usuario")
def alta_notificacion(peticion: AltaNotificacion, s: Session = Depends(sesion)):
    notificacion = NotificacionFila(
        id=nuevo_id(),
        usuarioId=peticion.usuarioId,
        tipo=peticion.tipo.upper(),
        canal=peticion.canal.upper(),
        contenido=peticion.contenido.strip(),
        fechaEnvio=datetime.now(),
        leida=False,
    )
    s.add(notificacion)
    s.commit()
    return Notificacion.model_validate(notificacion)


# --- Conversaciones --------------------------------------------------------
@app.get("/conversaciones", tags=["mensajería"], summary="Listar conversaciones")
def listar_conversaciones(
    contactoId: str | None = Query(None),
    contactoIds: str | None = Query(
        None, description="Varios contactos separados por coma; es como el gateway "
                          "pide de una vez todas las conversaciones de una persona"),
    participanteId: str | None = Query(None, description="usuarioId de quien ha escrito"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    lista_contactos = [c.strip() for c in contactoIds.split(",") if c.strip()] if contactoIds else None
    consulta = select(ConversacionFila).where(*condiciones(
        (ConversacionFila.contactoId == contactoId) if contactoId else None,
        ConversacionFila.contactoId.in_(lista_contactos) if lista_contactos else None,
        # Filtrar por participante solo encuentra hilos donde ya escribió. Sirve
        # para "mis conversaciones activas", pero deja fuera una recién abierta;
        # por eso la bandeja se pide por contactoIds.
        ConversacionFila.id.in_(
            select(MensajeFila.conversacionId).where(MensajeFila.remitenteId == participanteId)
        ) if participanteId else None,
    )).order_by(ConversacionFila.fechaInicio.desc())

    pagina = paginar_consulta(s, consulta, Conversacion, page, size)
    identificadores = [c.id for c in pagina.items]
    if not identificadores:
        return pagina

    # La lista muestra el conteo y el último mensaje de cada hilo. Se resuelve
    # con dos consultas para toda la página, no con dos por conversación.
    conteos = {
        fila.conversacionId: (fila.total, fila.no_leidos)
        for fila in s.execute(
            select(
                MensajeFila.conversacionId.label("conversacionId"),
                func.count().label("total"),
                func.count().filter(MensajeFila.leido.is_(False)).label("no_leidos"),
            )
            .where(MensajeFila.conversacionId.in_(identificadores))
            .group_by(MensajeFila.conversacionId)
        )
    }

    ultimas_fechas = (
        select(MensajeFila.conversacionId, func.max(MensajeFila.fechaEnvio).label("fecha"))
        .where(MensajeFila.conversacionId.in_(identificadores))
        .group_by(MensajeFila.conversacionId)
        .subquery()
    )
    ultimos = {
        fila.conversacionId: Mensaje.model_validate(fila)
        for fila in s.scalars(
            select(MensajeFila).join(
                ultimas_fechas,
                (MensajeFila.conversacionId == ultimas_fechas.c.conversacionId)
                & (MensajeFila.fechaEnvio == ultimas_fechas.c.fecha),
            )
        )
    }

    pagina.items = [{
        **conversacion.model_dump(),
        "totalMensajes": conteos.get(conversacion.id, (0, 0))[0],
        "noLeidos": conteos.get(conversacion.id, (0, 0))[1],
        "ultimoMensaje": ultimos.get(conversacion.id),
    } for conversacion in pagina.items]
    return pagina


@app.get("/conversaciones/{conversacion_id}", tags=["mensajería"], response_model=Conversacion,
         summary="Obtener una conversación")
def obtener_conversacion(conversacion_id: str, s: Session = Depends(sesion)):
    fila = s.get(ConversacionFila, conversacion_id)
    if fila is None:
        raise HTTPException(404, f"Conversacion '{conversacion_id}' no encontrada")
    return Conversacion.model_validate(fila)


@app.get("/conversaciones/{conversacion_id}/mensajes", tags=["mensajería"],
         summary="Mensajes de una conversación")
def mensajes_de_conversacion(
    conversacion_id: str,
    leido: bool | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    s: Session = Depends(sesion),
):
    if s.get(ConversacionFila, conversacion_id) is None:
        raise HTTPException(404, f"Conversacion '{conversacion_id}' no encontrada")
    consulta = select(MensajeFila).where(*condiciones(
        MensajeFila.conversacionId == conversacion_id,
        (MensajeFila.leido == leido) if leido is not None else None,
    )).order_by(MensajeFila.fechaEnvio)
    return paginar_consulta(s, consulta, Mensaje, page, size)


@app.get("/mensajes/{mensaje_id}", tags=["mensajería"], response_model=Mensaje,
         summary="Obtener un mensaje")
def obtener_mensaje(mensaje_id: str, s: Session = Depends(sesion)):
    fila = s.get(MensajeFila, mensaje_id)
    if fila is None:
        raise HTTPException(404, f"Mensaje '{mensaje_id}' no encontrado")
    return Mensaje.model_validate(fila)


# --- Notificaciones --------------------------------------------------------
@app.get("/notificaciones", tags=["notificaciones"], summary="Listar notificaciones")
def listar_notificaciones(
    usuarioId: str | None = Query(None),
    leida: bool | None = Query(None),
    tipo: str | None = Query(None),
    canal: str | None = Query(None, description="PUSH, EMAIL o SMS"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(NotificacionFila).where(*condiciones(
        (NotificacionFila.usuarioId == usuarioId) if usuarioId else None,
        (NotificacionFila.leida == leida) if leida is not None else None,
        (func.upper(NotificacionFila.tipo) == tipo.upper()) if tipo else None,
        (func.upper(NotificacionFila.canal) == canal.upper()) if canal else None,
    )).order_by(NotificacionFila.fechaEnvio.desc())
    return paginar_consulta(s, consulta, Notificacion, page, size)


@app.get("/notificaciones/resumen", tags=["notificaciones"],
         summary="Conteo de no leídas por canal para un usuario")
def resumen_notificaciones(
    usuarioId: str = Query(..., description="Usuario a consultar"),
    s: Session = Depends(sesion),
):
    total = s.scalar(
        select(func.count()).select_from(NotificacionFila)
        .where(NotificacionFila.usuarioId == usuarioId)
    ) or 0
    por_canal = dict(s.execute(
        select(NotificacionFila.canal, func.count())
        .where(NotificacionFila.usuarioId == usuarioId, NotificacionFila.leida.is_(False))
        .group_by(NotificacionFila.canal)
    ).all())
    return {
        "usuarioId": usuarioId,
        "total": total,
        "noLeidas": sum(por_canal.values()),
        "noLeidasPorCanal": por_canal,
    }


@app.get("/notificaciones/{notificacion_id}", tags=["notificaciones"], response_model=Notificacion,
         summary="Obtener una notificación")
def obtener_notificacion(notificacion_id: str, s: Session = Depends(sesion)):
    fila = s.get(NotificacionFila, notificacion_id)
    if fila is None:
        raise HTTPException(404, f"Notificacion '{notificacion_id}' no encontrada")
    return Notificacion.model_validate(fila)
