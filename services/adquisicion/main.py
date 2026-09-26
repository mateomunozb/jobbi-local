"""Contexto delimitado: Adquisición y Distribución.

Arranca vacío. Los aliados los da de alta el equipo comercial; mientras no haya
ninguno, los endpoints responden listas vacías en vez de datos inventados.

Dueño de los aliados que refieren prestadores a la plataforma (ferreterías,
juntas de acción comunal, cooperativas) y de la trazabilidad de esas
referencias, en la base `jobbi_adquisicion`. Guarda solo el UUID del prestador
referido, que vive en el contexto Identidad.
"""

from __future__ import annotations

from datetime import date

from fastapi import Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from common.db import condiciones, inicializar, listar, paginar_consulta
from common.esquemas import Esquema
from common.service import crear_servicio

from .tablas import AliadoDistribucionFila, ReferidoFila

app = crear_servicio(
    nombre="adquisicion",
    contexto="Adquisición y Distribución",
    descripcion=(
        "Aliados de distribución que refieren prestadores a JOBBI y la "
        "trazabilidad de cada referido por zona de cobertura."
    ),
)


class AliadoDistribucion(Esquema):
    id: str
    nombre: str
    tipo: str
    zonaCobertura: str


class Referido(Esquema):
    aliadoId: str
    prestadorId: str
    fechaReferencia: date
    activado: bool


Sesion: sessionmaker[Session] = inicializar("adquisicion")


def sesion() -> Session:
    with Sesion() as s:
        yield s


@app.get("/aliados-distribucion", tags=["aliados"], summary="Listar aliados de distribución")
def listar_aliados(
    tipo: str | None = Query(None, description="COMERCIO, JUNTA_ACCION_COMUNAL, COOPERATIVA"),
    zona: str | None = Query(None, description="Coincidencia parcial en la zona de cobertura"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(AliadoDistribucionFila).where(*condiciones(
        (func.upper(AliadoDistribucionFila.tipo) == tipo.upper()) if tipo else None,
        AliadoDistribucionFila.zonaCobertura.ilike(f"%{zona}%") if zona else None,
    )).order_by(AliadoDistribucionFila.nombre)
    return paginar_consulta(s, consulta, AliadoDistribucion, page, size)


@app.get("/aliados-distribucion/{aliado_id}", tags=["aliados"], response_model=AliadoDistribucion,
         summary="Obtener un aliado de distribución")
def obtener_aliado(aliado_id: str, s: Session = Depends(sesion)):
    fila = s.get(AliadoDistribucionFila, aliado_id)
    if fila is None:
        raise HTTPException(404, f"AliadoDistribucion '{aliado_id}' no encontrado")
    return AliadoDistribucion.model_validate(fila)


@app.get("/aliados-distribucion/{aliado_id}/referidos", tags=["referidos"],
         summary="Prestadores referidos por un aliado")
def referidos_de_aliado(
    aliado_id: str,
    activado: bool | None = Query(None),
    s: Session = Depends(sesion),
):
    if s.get(AliadoDistribucionFila, aliado_id) is None:
        raise HTTPException(404, f"AliadoDistribucion '{aliado_id}' no encontrado")
    consulta = select(ReferidoFila).where(*condiciones(
        ReferidoFila.aliadoId == aliado_id,
        (ReferidoFila.activado == activado) if activado is not None else None,
    ))
    return listar(s, consulta, Referido)


@app.get("/referidos", tags=["referidos"], summary="Listar referidos")
def listar_referidos(
    prestadorId: str | None = Query(None),
    aliadoId: str | None = Query(None),
    activado: bool | None = Query(None),
    s: Session = Depends(sesion),
):
    consulta = select(ReferidoFila).where(*condiciones(
        (ReferidoFila.prestadorId == prestadorId) if prestadorId else None,
        (ReferidoFila.aliadoId == aliadoId) if aliadoId else None,
        (ReferidoFila.activado == activado) if activado is not None else None,
    )).order_by(ReferidoFila.fechaReferencia.desc())
    return listar(s, consulta, Referido)


@app.get("/resumen/canal", tags=["resumen"], summary="Conversión de referidos por aliado")
def resumen_canal(s: Session = Depends(sesion)):
    # Un GROUP BY con conteo condicional: total de referidos y cuántos se
    # activaron, por aliado, sin traer las filas al proceso.
    filas = s.execute(
        select(
            AliadoDistribucionFila,
            func.count(ReferidoFila.prestadorId),
            func.count().filter(ReferidoFila.activado.is_(True)),
        )
        .outerjoin(ReferidoFila, ReferidoFila.aliadoId == AliadoDistribucionFila.id)
        .group_by(AliadoDistribucionFila.id)
        .order_by(AliadoDistribucionFila.nombre)
    ).all()
    items = [{
        "aliado": AliadoDistribucion.model_validate(aliado),
        "totalReferidos": total,
        "activados": activados,
        "tasaActivacion": round(activados / total, 2) if total else 0,
    } for aliado, total, activados in filas]
    return {"total": len(items), "items": items}
