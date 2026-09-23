"""Contexto delimitado: Confianza y Verificación.

Dueño de las verificaciones de identidad contra aliados externos y de las
reseñas moderadas, en la base `jobbi_confianza`. La calificación promedio del
perfil vive en Identidad; aquí está el detalle que la sustenta.
"""

from __future__ import annotations

from datetime import date

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from common.db import condiciones, inicializar, listar, nuevo_id, paginar_consulta
from common.enums import EstadoModeracion, EstadoVerificacion, valores
from common.service import crear_servicio

from .models import AliadoVerificacion, Resena, VerificacionIdentidad
from .tablas import AliadoVerificacionFila, ResenaFila, VerificacionIdentidadFila

app = crear_servicio(
    nombre="confianza",
    contexto="Confianza y Verificación",
    descripcion=(
        "Verificaciones de identidad ejecutadas por aliados externos y reseñas "
        "moderadas que alimentan la reputación de la plataforma."
    ),
)


Sesion: sessionmaker[Session] = inicializar("confianza")


def sesion() -> Session:
    with Sesion() as s:
        yield s


# --- Aliados de verificación ----------------------------------------------
@app.get("/aliados-verificacion", tags=["aliados"], response_model=list[AliadoVerificacion],
         summary="Listar aliados de verificación")
def listar_aliados(
    slaMaximoHoras: int | None = Query(None, ge=0),
    s: Session = Depends(sesion),
):
    consulta = select(AliadoVerificacionFila).where(*condiciones(
        (AliadoVerificacionFila.slaHoras <= slaMaximoHoras) if slaMaximoHoras is not None else None,
    )).order_by(AliadoVerificacionFila.slaHoras)
    return listar(s, consulta, AliadoVerificacion)


@app.get("/aliados-verificacion/{aliado_id}", tags=["aliados"], response_model=AliadoVerificacion,
         summary="Obtener un aliado de verificación")
def obtener_aliado(aliado_id: str, s: Session = Depends(sesion)):
    fila = s.get(AliadoVerificacionFila, aliado_id)
    if fila is None:
        raise HTTPException(404, f"AliadoVerificacion '{aliado_id}' no encontrado")
    return AliadoVerificacion.model_validate(fila)


# --- Verificaciones --------------------------------------------------------
@app.get("/verificaciones", tags=["verificaciones"], summary="Listar verificaciones de identidad")
def listar_verificaciones(
    prestadorId: str | None = Query(None),
    aliadoVerificacionId: str | None = Query(None),
    estado: EstadoVerificacion | None = Query(None),
    tipoVerificacion: str | None = Query(None, description="DOCUMENTO, ANTECEDENTES, BIOMETRIA"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(VerificacionIdentidadFila).where(*condiciones(
        (VerificacionIdentidadFila.prestadorId == prestadorId) if prestadorId else None,
        (VerificacionIdentidadFila.aliadoVerificacionId == aliadoVerificacionId)
        if aliadoVerificacionId else None,
        (VerificacionIdentidadFila.estado == estado.value) if estado else None,
        (func.upper(VerificacionIdentidadFila.tipoVerificacion) == tipoVerificacion.upper())
        if tipoVerificacion else None,
    )).order_by(VerificacionIdentidadFila.fechaSolicitud.desc())
    return paginar_consulta(s, consulta, VerificacionIdentidad, page, size)


@app.get("/verificaciones/{verificacion_id}", tags=["verificaciones"],
         response_model=VerificacionIdentidad, summary="Obtener una verificación")
def obtener_verificacion(verificacion_id: str, s: Session = Depends(sesion)):
    fila = s.get(VerificacionIdentidadFila, verificacion_id)
    if fila is None:
        raise HTTPException(404, f"VerificacionIdentidad '{verificacion_id}' no encontrada")
    return VerificacionIdentidad.model_validate(fila)


@app.get("/prestadores/{prestador_id}/estado-verificacion", tags=["verificaciones"],
         summary="Estado consolidado de verificación de un prestador")
def estado_verificacion(prestador_id: str, s: Session = Depends(sesion)):
    propias = select(VerificacionIdentidadFila).where(
        VerificacionIdentidadFila.prestadorId == prestador_id)

    por_estado = dict(s.execute(
        select(VerificacionIdentidadFila.estado, func.count())
        .where(VerificacionIdentidadFila.prestadorId == prestador_id)
        .group_by(VerificacionIdentidadFila.estado)
    ).all())
    aprobadas = por_estado.get(EstadoVerificacion.APROBADA.value, 0)
    pendientes = por_estado.get(EstadoVerificacion.PENDIENTE.value, 0)
    total = sum(por_estado.values())

    costo = s.scalar(
        select(func.coalesce(func.sum(VerificacionIdentidadFila.costo), 0))
        .where(VerificacionIdentidadFila.prestadorId == prestador_id)
    ) or 0
    tipos = s.scalars(
        select(VerificacionIdentidadFila.tipoVerificacion).distinct()
        .where(VerificacionIdentidadFila.prestadorId == prestador_id,
               VerificacionIdentidadFila.estado == EstadoVerificacion.APROBADA.value)
    ).all()

    return {
        "prestadorId": prestador_id,
        "totalVerificaciones": total,
        "aprobadas": aprobadas,
        "pendientes": pendientes,
        "rechazadas": total - aprobadas - pendientes,
        # La insignia se otorga solo cuando no queda nada pendiente ni rechazado.
        "insigniaVigente": aprobadas > 0 and aprobadas == total,
        "costoAcumulado": float(costo),
        "tiposAprobados": sorted(tipos),
    }


# --- Reseñas ---------------------------------------------------------------

class AltaResena(BaseModel):
    contratacionId: str
    autorId: str
    receptorId: str
    puntuacion: int = Field(ge=1, le=5)
    comentario: str = Field(default="", max_length=2000)


@app.post("/resenas", tags=["reseñas"], status_code=201,
          summary="Publicar la reseña de una contratación terminada")
def alta_resena(peticion: AltaResena, s: Session = Depends(sesion)):
    # Una reseña por autor y contratación: calificar de nuevo corrige la que ya
    # escribió, no añade una segunda voz del mismo participante.
    existente = s.scalars(select(ResenaFila).where(
        ResenaFila.contratacionId == peticion.contratacionId,
        ResenaFila.autorId == peticion.autorId,
    )).first()
    if existente:
        existente.puntuacion = peticion.puntuacion
        existente.comentario = peticion.comentario.strip()
        existente.fecha = date.today()
        s.commit()
        return Resena.model_validate(existente)

    resena = ResenaFila(
        id=nuevo_id(),
        contratacionId=peticion.contratacionId,
        autorId=peticion.autorId,
        receptorId=peticion.receptorId,
        puntuacion=peticion.puntuacion,
        comentario=peticion.comentario.strip(),
        fecha=date.today(),
        # En esta fase no hay moderación humana: la reseña se publica y cuenta
        # de inmediato para el promedio del perfil.
        estadoModeracion=EstadoModeracion.APROBADA.value,
    )
    s.add(resena)
    s.commit()
    return Resena.model_validate(resena)


@app.get("/resenas", tags=["reseñas"], summary="Listar reseñas")
def listar_resenas(
    receptorId: str | None = Query(None),
    autorId: str | None = Query(None),
    contratacionId: str | None = Query(None),
    estadoModeracion: EstadoModeracion | None = Query(None),
    puntuacionMinima: int | None = Query(None, ge=1, le=5),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(ResenaFila).where(*condiciones(
        (ResenaFila.receptorId == receptorId) if receptorId else None,
        (ResenaFila.autorId == autorId) if autorId else None,
        (ResenaFila.contratacionId == contratacionId) if contratacionId else None,
        (ResenaFila.estadoModeracion == estadoModeracion.value) if estadoModeracion else None,
        (ResenaFila.puntuacion >= puntuacionMinima) if puntuacionMinima is not None else None,
    )).order_by(ResenaFila.fecha.desc())
    return paginar_consulta(s, consulta, Resena, page, size)


@app.get("/resenas/resumen", tags=["reseñas"],
         summary="Promedio y distribución de puntuaciones de un receptor")
def resumen_resenas(
    receptorId: str = Query(..., description="Perfil que recibe las reseñas"),
    soloAprobadas: bool = Query(True, description="Excluye reseñas pendientes o rechazadas"),
    s: Session = Depends(sesion),
):
    filtros = condiciones(
        ResenaFila.receptorId == receptorId,
        (ResenaFila.estadoModeracion == EstadoModeracion.APROBADA.value) if soloAprobadas else None,
    )
    # La distribución sale de un GROUP BY por puntuación; las estrellas sin
    # reseñas se completan en cero para que el gráfico tenga siempre 5 barras.
    conteos = dict(s.execute(
        select(ResenaFila.puntuacion, func.count()).where(*filtros).group_by(ResenaFila.puntuacion)
    ).all())
    distribucion = {estrella: conteos.get(estrella, 0) for estrella in range(1, 6)}
    total = sum(distribucion.values())
    suma = sum(estrella * n for estrella, n in distribucion.items())
    return {
        "receptorId": receptorId,
        "total": total,
        "promedio": round(suma / total, 2) if total else 0,
        "distribucion": distribucion,
    }


@app.get("/resenas/{resena_id}", tags=["reseñas"], response_model=Resena,
         summary="Obtener una reseña")
def obtener_resena(resena_id: str, s: Session = Depends(sesion)):
    fila = s.get(ResenaFila, resena_id)
    if fila is None:
        raise HTTPException(404, f"Resena '{resena_id}' no encontrada")
    return Resena.model_validate(fila)


@app.get("/moderacion/cola", tags=["reseñas"], summary="Reseñas pendientes de moderación")
def cola_moderacion(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = (select(ResenaFila)
                .where(ResenaFila.estadoModeracion == EstadoModeracion.PENDIENTE.value)
                .order_by(ResenaFila.fecha))
    return paginar_consulta(s, consulta, Resena, page, size)


@app.get("/enums", tags=["catálogos"], summary="Enumeraciones que expone este contexto")
def enumeraciones():
    return {
        "estadoVerificacion": valores(EstadoVerificacion),
        "estadoModeracion": valores(EstadoModeracion),
    }
