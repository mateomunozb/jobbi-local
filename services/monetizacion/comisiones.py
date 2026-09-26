"""Cálculo de la comisión de la plataforma (patrón Strategy).

Hay dos momentos distintos en la vida de una comisión y cada uno usa su propia
estrategia:

- **Cotizar** (al cerrar un acuerdo): la comisión depende del plan que el
  prestador tiene *hoy*. `ComisionPlanFree` y `ComisionPlanPro` son la tarifa
  vigente de cada plan; `/planes` las publica y el gateway congela ese
  porcentaje en la contratación.
- **Cobrar** (al consumir CONTRATACION_COMPLETADA): rige el porcentaje que quedó
  congelado al contratar (RN-05, RN-06), aunque después el prestador haya
  cambiado de plan o la tarifa del plan haya cambiado. Eso es
  `ComisionCongelada`.

Quien calcula no sabe cuál de ellas tiene: solo llama `calcular`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from common.enums import PlanPrestador


class EstrategiaComision(ABC):
    porcentaje: float

    @abstractmethod
    def calcular(self, valor_acordado: float) -> float:
        """Monto de la comisión, en pesos, para un servicio de `valor_acordado`."""


class _ComisionPorcentual(EstrategiaComision):
    def calcular(self, valor_acordado: float) -> float:
        # Mismo redondeo con el que Contrataciones guarda `montoComision`.
        return round(valor_acordado * self.porcentaje, 2)


class ComisionPlanFree(_ComisionPorcentual):
    plan = PlanPrestador.FREE.value
    porcentaje = 0.18


class ComisionPlanPro(_ComisionPorcentual):
    plan = PlanPrestador.PRO.value
    porcentaje = 0.12


class ComisionCongelada(_ComisionPorcentual):
    """El porcentaje que se pactó al contratar, sin importar el plan de hoy."""

    def __init__(self, porcentaje: float) -> None:
        self.porcentaje = porcentaje


# Tarifa vigente de cada plan: la que se cotiza al cerrar un acuerdo nuevo.
ESTRATEGIAS_POR_PLAN: dict[str, EstrategiaComision] = {
    e.plan: e for e in (ComisionPlanFree(), ComisionPlanPro())
}


def estrategia_de_cobro(evento: dict[str, Any]) -> EstrategiaComision | None:
    """Estrategia con la que se cobra una contratación completada.

    Si el evento trae el porcentaje congelado, ese manda. Si solo trae el plan
    (productores antiguos), se usa la tarifa de ese plan. Si no trae ninguno,
    devuelve None y el llamador cobra el `montoComision` tal como llegó.
    """
    porcentaje = evento.get("porcentajeComisionAplicado")
    if porcentaje is not None:
        return ComisionCongelada(float(porcentaje))
    return ESTRATEGIAS_POR_PLAN.get(str(evento.get("planPrestador", "")).upper())
