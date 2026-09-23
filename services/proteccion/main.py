"""Contexto delimitado: Protección / Seguros.

El modelo de dominio lo marca como fuera de construcción en Fase 1. Se expone
igual, en solo lectura, para que la frontera quede reservada y el frontend
pueda consultarla sin que la Fase 2 obligue a rediseñar el contrato.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from common.db import condiciones, inicializar, listar, paginar_consulta
from common.esquemas import Esquema
from common.service import crear_servicio

from .tablas import AseguradoraFila, PlanProteccionFila

app = crear_servicio(
    nombre="proteccion",
    contexto="Protección / Seguros",
    descripcion=(
        "Aseguradoras y planes de protección asociados a una contratación. "
        "Contexto reservado: fuera de construcción en Fase 1, solo consulta."
    ),
)


class Aseguradora(Esquema):
    id: str
    nombre: str
    vigiladoPor: str


class PlanProteccion(Esquema):
    id: str
    aseguradoraId: str
    contratacionId: str | None = None
    prima: float
    cobertura: str
    estado: str


Sesion: sessionmaker[Session] = inicializar("proteccion")


def sesion() -> Session:
    with Sesion() as s:
        yield s


@app.get("/aseguradoras", tags=["aseguradoras"], response_model=list[Aseguradora],
         summary="Listar aseguradoras aliadas")
def listar_aseguradoras(s: Session = Depends(sesion)):
    return listar(s, select(AseguradoraFila).order_by(AseguradoraFila.nombre), Aseguradora)


@app.get("/aseguradoras/{aseguradora_id}", tags=["aseguradoras"], response_model=Aseguradora,
         summary="Obtener una aseguradora")
def obtener_aseguradora(aseguradora_id: str, s: Session = Depends(sesion)):
    fila = s.get(AseguradoraFila, aseguradora_id)
    if fila is None:
        raise HTTPException(404, f"Aseguradora '{aseguradora_id}' no encontrada")
    return Aseguradora.model_validate(fila)


@app.get("/planes-proteccion", tags=["planes"], summary="Listar planes de protección")
def listar_planes(
    contratacionId: str | None = Query(None),
    aseguradoraId: str | None = Query(None),
    estado: str | None = Query(None, description="VIGENTE, DISPONIBLE, VENCIDO"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(PlanProteccionFila).where(*condiciones(
        (PlanProteccionFila.contratacionId == contratacionId) if contratacionId else None,
        (PlanProteccionFila.aseguradoraId == aseguradoraId) if aseguradoraId else None,
        (func.upper(PlanProteccionFila.estado) == estado.upper()) if estado else None,
    ))
    return paginar_consulta(s, consulta, PlanProteccion, page, size)


@app.get("/planes-proteccion/{plan_id}", tags=["planes"], response_model=PlanProteccion,
         summary="Obtener un plan de protección")
def obtener_plan(plan_id: str, s: Session = Depends(sesion)):
    fila = s.get(PlanProteccionFila, plan_id)
    if fila is None:
        raise HTTPException(404, f"PlanProteccion '{plan_id}' no encontrado")
    return PlanProteccion.model_validate(fila)
