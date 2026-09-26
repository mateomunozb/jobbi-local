"""Reglas de negocio de Monetización como objetos evaluables (patrón Specification).

Una especificación responde una sola pregunta sobre un objeto del dominio. Tener
la regla aquí, y no escrita en línea donde se usa, permite evaluarla igual desde
el cobro, desde una consulta o desde una prueba, y cambiar el umbral en un solo
lugar.
"""

from __future__ import annotations

from typing import Protocol

# A partir de este saldo la billetera se bloquea: la comisión en efectivo la
# cobra el prestador de su cliente y queda debiéndosela a la plataforma, así que
# acumular deuda sin liquidar no puede ser gratis.
UMBRAL_BLOQUEO = 150000.0


class ConSaldoPendiente(Protocol):
    saldoPendiente: float


class EspecificacionPrestadorBloqueado:
    """RN-04: un prestador queda bloqueado cuando su saldo pendiente alcanza el umbral.

    El umbral exacto ya bloquea (`>=`): es el máximo que la plataforma admite
    fiar, no el primer valor que se tolera.
    """

    def __init__(self, umbral: float = UMBRAL_BLOQUEO) -> None:
        self.umbral = umbral

    def es_satisfecha_por(self, billetera: ConSaldoPendiente) -> bool:
        return billetera.saldoPendiente >= self.umbral
