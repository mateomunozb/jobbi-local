"""Contexto delimitado: Comunicación.

Dueño de la mensajería (Conversación, Mensaje) y de las notificaciones, en la
base `jobbi_comunicacion`. Las conversaciones cuelgan de un Contacto del
contexto Mercado, referenciado solo por su UUID.

Hay un chat por servicio: al cerrarse el servicio su conversación queda
CERRADA (solo lectura) y volver a contactar abre una nueva. Los mensajes viajan
en tiempo real por WebSocket (`/ws/conversaciones/{id}`, ver `salas.py`).
"""

from __future__ import annotations

from datetime import date, datetime

import anyio
from fastapi import Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from common.db import condiciones, inicializar, nuevo_id, paginar_consulta
from common.service import crear_servicio

from .models import Conversacion, Mensaje, Notificacion
from .salas import salas
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
          summary="Abrir el chat de un contacto (reusa el abierto, o crea uno nuevo)")
def alta_conversacion(peticion: AltaConversacion, s: Session = Depends(sesion)):
    # Un chat por servicio: mientras haya uno ABIERTO con este contacto se
    # devuelve ese (contactar dos veces no duplica). Si todos están cerrados,
    # es un servicio nuevo y empieza un chat nuevo.
    abierta = s.scalars(select(ConversacionFila).where(
        ConversacionFila.contactoId == peticion.contactoId,
        ConversacionFila.estado == "ABIERTA",
    )).first()
    if abierta:
        return Conversacion.model_validate(abierta)

    ahora = datetime.now()
    conversacion = ConversacionFila(
        id=nuevo_id(), contactoId=peticion.contactoId, fechaInicio=ahora.date(),
        estado="ABIERTA", creadaEn=ahora,
    )
    s.add(conversacion)
    s.commit()
    return Conversacion.model_validate(conversacion)


class CierreConversacion(BaseModel):
    contratacionId: str | None = None


@app.post("/conversaciones/{conversacion_id}/cerrar", tags=["mensajería"],
          summary="Cerrar el chat al terminar su servicio (queda de solo lectura)")
def cerrar_conversacion(conversacion_id: str, peticion: CierreConversacion,
                        s: Session = Depends(sesion)):
    fila = s.get(ConversacionFila, conversacion_id)
    if fila is None:
        raise HTTPException(404, f"Conversacion '{conversacion_id}' no encontrada")
    if fila.estado != "CERRADA":
        fila.estado = "CERRADA"
        fila.fechaCierre = datetime.now()
        fila.contratacionId = peticion.contratacionId
        s.commit()
        # Quien tenga el chat abierto lo ve deshabilitarse en el momento.
        _difundir(conversacion_id, {"tipo": "cerrada",
                                    "conversacion": jsonable_encoder(Conversacion.model_validate(fila))})
    return Conversacion.model_validate(fila)


def _crear_mensaje(s: Session, conversacion_id: str, remitente_id: str, contenido: str) -> Mensaje:
    """Guarda un mensaje. La usan el POST y el WebSocket, con las mismas reglas."""
    conversacion = s.get(ConversacionFila, conversacion_id)
    if conversacion is None:
        raise HTTPException(404, f"Conversacion '{conversacion_id}' no encontrada")
    if conversacion.estado == "CERRADA":
        raise HTTPException(409, "Este chat se cerró al terminar el servicio")
    contenido = contenido.strip()
    if not contenido or len(contenido) > 2000:
        raise HTTPException(422, "El mensaje debe tener entre 1 y 2000 caracteres")

    mensaje = MensajeFila(
        id=nuevo_id(),
        conversacionId=conversacion_id,
        remitenteId=remitente_id,
        contenido=contenido,
        fechaEnvio=datetime.now(),
        # Lo escribe quien lo envía, así que nace sin leer para el destinatario.
        leido=False,
    )
    s.add(mensaje)
    s.commit()
    return Mensaje.model_validate(mensaje)


def _difundir(conversacion_id: str, evento: dict) -> None:
    """Difunde a la sala desde un endpoint síncrono (corre en un hilo aparte)."""
    try:
        anyio.from_thread.run(salas.difundir, conversacion_id, evento)
    except RuntimeError:
        # Fuera de un hilo gestionado por anyio no hay sala a quien avisar.
        pass


@app.post("/mensajes", tags=["mensajería"], status_code=201, summary="Enviar un mensaje")
def alta_mensaje(peticion: AltaMensaje, s: Session = Depends(sesion)):
    mensaje = _crear_mensaje(s, peticion.conversacionId, peticion.remitenteId, peticion.contenido)
    # Un mensaje enviado por HTTP también llega en vivo a quien tenga el chat abierto.
    _difundir(peticion.conversacionId, {"tipo": "mensaje", "mensaje": jsonable_encoder(mensaje)})
    return mensaje


# --- Chat en tiempo real ---------------------------------------------------

def _con_sesion(funcion, *args):
    with Sesion() as s:
        return funcion(s, *args)


def _conversacion(s: Session, conversacion_id: str) -> ConversacionFila | None:
    return s.get(ConversacionFila, conversacion_id)


@app.websocket("/ws/conversaciones/{conversacion_id}")
async def chat_en_vivo(ws: WebSocket, conversacion_id: str, usuarioId: str = Query(...)):
    """Sala de chat de una conversación.

    Del cliente llegan `{"tipo": "mensaje", "contenido": "..."}` y
    `{"tipo": "escribiendo"}`. A la sala se difunden `mensaje` (a todos,
    también a quien lo envió), `escribiendo` (a los demás), `presencia` (cuántos
    están conectados) y `cerrada` (cuando el servicio se cierra).
    """
    conversacion = await run_in_threadpool(_con_sesion, _conversacion, conversacion_id)
    # Se acepta antes de rechazar: cerrar sin aceptar responde un HTTP 403 y el
    # navegador solo ve un error genérico. Aceptada, el código 4404 le llega
    # intacto (atravesando gateway y Next) y sabe que no debe reintentar.
    await ws.accept()
    if conversacion is None:
        await ws.close(code=4404, reason="Conversación no encontrada")
        return

    conectados = salas.unir(conversacion_id, ws)
    await ws.send_json({"tipo": "conectado", "estado": conversacion.estado, "enLinea": conectados})
    await salas.difundir(conversacion_id, {"tipo": "presencia", "enLinea": conectados}, excepto=ws)
    try:
        while True:
            datos = await ws.receive_json()
            tipo = datos.get("tipo") if isinstance(datos, dict) else None
            if tipo == "mensaje":
                try:
                    mensaje = await run_in_threadpool(
                        _con_sesion, _crear_mensaje, conversacion_id, usuarioId, str(datos.get("contenido", "")))
                except HTTPException as e:
                    await ws.send_json({"tipo": "error", "detalle": e.detail, "codigo": e.status_code})
                    continue
                await salas.difundir(conversacion_id, {"tipo": "mensaje", "mensaje": jsonable_encoder(mensaje)})
            elif tipo == "escribiendo":
                await salas.difundir(conversacion_id, {"tipo": "escribiendo", "usuarioId": usuarioId}, excepto=ws)
    except (WebSocketDisconnect, ValueError):
        # Desconexión normal, o un cliente que mandó algo que no es JSON.
        pass
    finally:
        quedan = salas.salir(conversacion_id, ws)
        await salas.difundir(conversacion_id, {"tipo": "presencia", "enLinea": quedan})


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
    estado: str | None = Query(None, description="ABIERTA o CERRADA"),
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
        (ConversacionFila.estado == estado.upper()) if estado else None,
    )).order_by(ConversacionFila.fechaInicio.desc(), ConversacionFila.creadaEn.desc().nulls_last())

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
