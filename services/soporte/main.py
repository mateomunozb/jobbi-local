"""Contexto delimitado: Soporte y Disputas.

Dueño de los incidentes abiertos sobre una contratación, en la base
`jobbi_soporte`. Es un contexto pequeño, así que esquema, siembra y rutas viven
en un solo módulo; las tablas están en `tablas.py`.
"""

from __future__ import annotations

from datetime import date

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from common.db import condiciones, inicializar, nuevo_id, paginar_consulta
from common.enums import EstadoIncidente, valores
from common.esquemas import Esquema
from common.service import crear_servicio

from .tablas import IncidenteFila

app = crear_servicio(
    nombre="soporte",
    contexto="Soporte y Disputas",
    descripcion=(
        "Incidentes reportados sobre una contratación, su investigación y "
        "resolución. Un incidente abierto puede bloquear la billetera."
    ),
)


class Incidente(Esquema):
    id: str
    contratacionId: str
    tipo: str
    estado: EstadoIncidente
    evidenciaDescripcion: str
    resolucion: str | None = None
    fechaApertura: date
    fechaCierre: date | None = None


Sesion: sessionmaker[Session] = inicializar("soporte")


def sesion() -> Session:
    with Sesion() as s:
        yield s


class AltaIncidente(BaseModel):
    contratacionId: str
    tipo: str = Field(min_length=2, max_length=40)
    evidenciaDescripcion: str = Field(min_length=1, max_length=2000)


@app.post("/incidentes", tags=["incidentes"], status_code=201,
          summary="Reportar un incidente sobre una contratación")
def alta_incidente(peticion: AltaIncidente, s: Session = Depends(sesion)):
    # Un caso por contratación: un segundo reporte sobre el mismo servicio se
    # añade a la investigación abierta, no abre otra.
    if s.scalars(select(IncidenteFila).where(
        IncidenteFila.contratacionId == peticion.contratacionId,
    )).first():
        raise HTTPException(409, "Ya hay un incidente reportado para este servicio")

    incidente = IncidenteFila(
        id=nuevo_id(),
        contratacionId=peticion.contratacionId,
        tipo=peticion.tipo.upper().replace(" ", "_"),
        # Todo reporte entra por ABIERTO; el equipo de soporte lo mueve de ahí.
        estado=EstadoIncidente.ABIERTO.value,
        evidenciaDescripcion=peticion.evidenciaDescripcion.strip(),
        resolucion=None,
        fechaApertura=date.today(),
        fechaCierre=None,
    )
    s.add(incidente)
    s.commit()
    return Incidente.model_validate(incidente)


class EvidenciaInvestigacion(BaseModel):
    """Lo que respalda la investigación (RN-10).

    El check-in y el check-out viven en Contrataciones: el gateway los consulta
    y los manda aquí, este contexto no cruza la frontera para averiguarlos.
    """

    checkInRegistrado: bool = False
    checkOutRegistrado: bool = False
    denunciaFormal: str | None = Field(default=None, max_length=4000)


def hay_evidencia(evidencia: EvidenciaInvestigacion) -> bool:
    """RN-10: check-in/check-out registrado en la contratación, o una denuncia formal documentada."""
    denuncia = (evidencia.denunciaFormal or "").strip()
    return evidencia.checkInRegistrado or evidencia.checkOutRegistrado or bool(denuncia)


@app.post("/incidentes/{incidente_id}/investigar", tags=["incidentes"], response_model=Incidente,
          summary="Pasar un incidente de ABIERTO a EN_INVESTIGACION (exige evidencia)")
def investigar_incidente(incidente_id: str, evidencia: EvidenciaInvestigacion, s: Session = Depends(sesion)):
    fila = s.get(IncidenteFila, incidente_id)
    if fila is None:
        raise HTTPException(404, f"Incidente '{incidente_id}' no encontrado")
    if fila.estado != EstadoIncidente.ABIERTO.value:
        raise HTTPException(409, f"Solo un incidente ABIERTO pasa a investigación; este está '{fila.estado}'")
    if not hay_evidencia(evidencia):
        raise HTTPException(409, "Sin evidencia no se investiga (RN-10): se requiere check-in/check-out "
                                 "registrado en la contratación o una denuncia formal documentada")
    if evidencia.denunciaFormal and evidencia.denunciaFormal.strip():
        fila.evidenciaDescripcion += f"\n\nDenuncia formal: {evidencia.denunciaFormal.strip()}"
    fila.estado = EstadoIncidente.EN_INVESTIGACION.value
    s.commit()
    return Incidente.model_validate(fila)


@app.get("/incidentes", tags=["incidentes"], summary="Listar incidentes")
def listar_incidentes(
    contratacionId: str | None = Query(None),
    estado: EstadoIncidente | None = Query(None),
    tipo: str | None = Query(None),
    abiertos: bool | None = Query(None, description="true = aún sin fecha de cierre"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(IncidenteFila).where(*condiciones(
        (IncidenteFila.contratacionId == contratacionId) if contratacionId else None,
        (IncidenteFila.estado == estado.value) if estado else None,
        (func.upper(IncidenteFila.tipo) == tipo.upper()) if tipo else None,
        (IncidenteFila.fechaCierre.is_(None) if abiertos else IncidenteFila.fechaCierre.is_not(None))
        if abiertos is not None else None,
    )).order_by(IncidenteFila.fechaApertura.desc())
    return paginar_consulta(s, consulta, Incidente, page, size)


@app.get("/incidentes/resumen", tags=["incidentes"], summary="Conteo por estado y tipo")
def resumen_incidentes(
    contratacionId: str | None = Query(None),
    s: Session = Depends(sesion),
):
    filtros = condiciones(
        (IncidenteFila.contratacionId == contratacionId) if contratacionId else None,
    )
    por_estado = dict(s.execute(
        select(IncidenteFila.estado, func.count()).where(*filtros)
        .group_by(IncidenteFila.estado)
    ).all())
    por_tipo = dict(s.execute(
        select(IncidenteFila.tipo, func.count()).where(*filtros).group_by(IncidenteFila.tipo)
    ).all())

    # Los días de cierre se calculan en Python porque la resta de fechas se
    # escribe distinto en PostgreSQL y en SQLite, y el conjunto es pequeño.
    cerrados = s.execute(
        select(IncidenteFila.fechaApertura, IncidenteFila.fechaCierre)
        .where(*condiciones(*filtros, IncidenteFila.fechaCierre.is_not(None)))
    ).all()
    total = sum(por_estado.values())
    return {
        "total": total,
        "abiertos": total - len(cerrados),
        "porEstado": por_estado,
        "porTipo": por_tipo,
        "diasPromedioCierre": (
            round(sum((cierre - apertura).days for apertura, cierre in cerrados) / len(cerrados), 2)
            if cerrados else None
        ),
    }


@app.get("/incidentes/{incidente_id}", tags=["incidentes"], response_model=Incidente,
         summary="Obtener un incidente")
def obtener_incidente(incidente_id: str, s: Session = Depends(sesion)):
    fila = s.get(IncidenteFila, incidente_id)
    if fila is None:
        raise HTTPException(404, f"Incidente '{incidente_id}' no encontrado")
    return Incidente.model_validate(fila)


@app.get("/enums", tags=["catálogos"], summary="Enumeraciones que expone este contexto")
def enumeraciones():
    return {"estadoIncidente": valores(EstadoIncidente)}
