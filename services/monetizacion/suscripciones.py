"""Vigencia de la suscripción Pro (RN-06).

Un prestador tiene a lo sumo una SuscripcionPro ACTIVA. Cuando pasa su
`fechaRenovacion` sin renovarse queda VENCIDA y el prestador vuelve a FREE: la
comisión sube de 12 % a 18 % desde la siguiente contratación (las ya acordadas
conservan su porcentaje congelado, RN-05).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.enums import PlanPrestador

from .tablas import SuscripcionProFila

ACTIVA, VENCIDA = "ACTIVA", "VENCIDA"


def vencida(suscripcion: SuscripcionProFila, hoy: date) -> bool:
    return suscripcion.estado == ACTIVA and suscripcion.fechaRenovacion < hoy


def plan_vigente(suscripcion: SuscripcionProFila | None, hoy: date) -> PlanPrestador:
    """PRO solo con una suscripción ACTIVA y no vencida; cualquier otra cosa es FREE."""
    if suscripcion is not None and suscripcion.estado == ACTIVA and not vencida(suscripcion, hoy):
        return PlanPrestador.PRO
    return PlanPrestador.FREE


def vencer(s: Session, hoy: date, prestador_id: str | None = None) -> list[str]:
    """Marca VENCIDAS las ACTIVAS cuya renovación ya pasó. No hace commit.

    Devuelve los prestadores que volvieron a FREE.
    """
    consulta = select(SuscripcionProFila).where(
        SuscripcionProFila.estado == ACTIVA, SuscripcionProFila.fechaRenovacion < hoy)
    if prestador_id:
        consulta = consulta.where(SuscripcionProFila.prestadorId == prestador_id)
    vencidas = s.scalars(consulta).all()
    for suscripcion in vencidas:
        suscripcion.estado = VENCIDA
    return [suscripcion.prestadorId for suscripcion in vencidas]
