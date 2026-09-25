"""Ciclo de vida de la Contratación (patrón State, RN-02).

Cada estado sabe qué transiciones admite; las que no admite las rechaza con
`TransicionInvalida`. Así ningún endpoint decide con condicionales sueltos si
un salto es válido, y un estado nuevo se agrega en un solo lugar.

Flujo real:

    SOLICITADA / ACEPTADA ──check-in──► CHECK_IN (en curso) ──check-out──► COMPLETADA

`CHECK_IN` es la forma concreta de "en curso": el prestador ya llegó. Los
estados `EN_CURSO` y `CHECK_OUT` del enum se tratan igual que `CHECK_IN` por si
algún registro los tiene. `COMPLETADA`, `CANCELADA` y `EN_DISPUTA` son
terminales: no admiten ni check-in ni cierre.
"""

from __future__ import annotations

from datetime import date, datetime

from common.enums import EstadoContratacion

from .models import Contratacion


def _estado(c: Contratacion) -> str:
    return EstadoContratacion(c.estado).value


class TransicionInvalida(Exception):
    """La contratación no admite esa transición en su estado actual."""


class EstadoDeContratacion:
    """Estado base: no admite ninguna transición."""

    def iniciar(self, c: Contratacion) -> None:
        raise TransicionInvalida(f"No se puede hacer check-in con la contratación en '{_estado(c)}'")

    def completar(self, c: Contratacion) -> None:
        if c.checkIn is None:
            raise TransicionInvalida("Hay que hacer check-in antes de cerrar el servicio")
        raise TransicionInvalida(f"No se puede cerrar el servicio con la contratación en '{_estado(c)}'")


class PorIniciar(EstadoDeContratacion):
    """SOLICITADA o ACEPTADA: el servicio está acordado y el prestador no ha llegado."""

    def iniciar(self, c: Contratacion) -> None:
        c.checkIn = datetime.now()
        c.fechaEjecucion = date.today()
        c.estado = EstadoContratacion.CHECK_IN


class EnCurso(EstadoDeContratacion):
    """El prestador hizo check-in: lo único que queda es cerrar el servicio."""

    def completar(self, c: Contratacion) -> None:
        if c.checkIn is None:
            super().completar(c)
        c.checkOut = datetime.now()
        # El check-out es el cierre del trabajo: no queda ningún paso intermedio
        # entre marcar la salida y dar el servicio por completado.
        c.estado = EstadoContratacion.COMPLETADA


class Terminal(EstadoDeContratacion):
    """COMPLETADA, CANCELADA o EN_DISPUTA: fuera del flujo de ejecución."""


_POR_INICIAR, _EN_CURSO, _TERMINAL = PorIniciar(), EnCurso(), Terminal()

_ESTADOS: dict[str, EstadoDeContratacion] = {
    EstadoContratacion.SOLICITADA.value: _POR_INICIAR,
    EstadoContratacion.ACEPTADA.value: _POR_INICIAR,
    EstadoContratacion.EN_CURSO.value: _EN_CURSO,
    EstadoContratacion.CHECK_IN.value: _EN_CURSO,
    EstadoContratacion.CHECK_OUT.value: _EN_CURSO,
    EstadoContratacion.COMPLETADA.value: _TERMINAL,
    EstadoContratacion.CANCELADA.value: _TERMINAL,
    EstadoContratacion.EN_DISPUTA.value: _TERMINAL,
}


def estado_de(c: Contratacion) -> EstadoDeContratacion:
    return _ESTADOS.get(_estado(c), _TERMINAL)
