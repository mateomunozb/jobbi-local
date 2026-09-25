"""Patrones de resiliencia compartidos: Circuit Breaker y backoff exponencial.

**Circuit Breaker.** Envuelve las llamadas a una dependencia externa. Tras
`umbral_fallos` fallos seguidos se *abre*: durante `espera_segundos` no se
llama a la dependencia y se responde de inmediato con el fallback, en lugar de
dejar hilos colgados esperando a un aliado lento. Pasado ese tiempo queda
*semiabierto*: deja pasar una llamada de prueba; si sale bien se cierra, si
falla se vuelve a abrir.

    CERRADO ──N fallos──► ABIERTO ──espera──► SEMIABIERTO ──éxito──► CERRADO
                             ▲                     │
                             └──────fallo──────────┘

**Backoff exponencial.** Espera 1, 2, 4, 8… segundos (con un tope y algo de
azar para que varios hilos no reintenten a la vez) entre intentos contra un
recurso caído, en vez de martillarlo a ritmo fijo.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, TypeVar

from prometheus_client import Counter, Gauge
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as TimeoutDelPool

from .observabilidad import log

T = TypeVar("T")

CERRADO, SEMIABIERTO, ABIERTO = "CERRADO", "SEMIABIERTO", "ABIERTO"
_VALOR_ESTADO = {CERRADO: 0, SEMIABIERTO: 1, ABIERTO: 2}

ESTADO_CIRCUITO = Gauge(
    "jobbi_circuit_breaker_estado",
    "Estado del circuito hacia una dependencia (0 cerrado, 1 semiabierto, 2 abierto)",
    ["dependencia"],
)
LLAMADAS_DEPENDENCIA = Counter(
    "jobbi_dependencia_externa_llamadas_total",
    "Llamadas a una dependencia externa por resultado (exito, fallo, rechazada_por_circuito)",
    ["dependencia", "resultado"],
)


class CircuitoAbierto(Exception):
    """El circuito está abierto: no se llamó a la dependencia."""


class CircuitBreaker:
    def __init__(self, dependencia: str, umbral_fallos: int = 3, espera_segundos: float = 30.0,
                 reloj: Callable[[], float] = time.monotonic) -> None:
        self.dependencia = dependencia
        self.umbral_fallos = umbral_fallos
        self.espera_segundos = espera_segundos
        self._reloj = reloj
        self._candado = threading.Lock()
        self._fallos = 0
        self._abierto_desde: float | None = None
        self._estado = CERRADO
        self._prueba_en_curso = False
        ESTADO_CIRCUITO.labels(dependencia).set(0)

    @property
    def estado(self) -> str:
        with self._candado:
            return self._estado_actual()

    def _estado_actual(self) -> str:
        if self._estado == ABIERTO and self._reloj() - (self._abierto_desde or 0) >= self.espera_segundos:
            self._cambiar(SEMIABIERTO)
        return self._estado

    def _cambiar(self, nuevo: str) -> None:
        if nuevo != self._estado:
            log.warning("circuit_breaker_cambio", extra={
                "dependencia": self.dependencia, "de": self._estado, "a": nuevo, "fallosSeguidos": self._fallos})
        self._estado = nuevo
        ESTADO_CIRCUITO.labels(self.dependencia).set(_VALOR_ESTADO[nuevo])

    def llamar(self, operacion: Callable[[], T]) -> T:
        """Ejecuta la operación a través del circuito. Lanza CircuitoAbierto si no la intentó."""
        with self._candado:
            estado = self._estado_actual()
            if estado == ABIERTO or (estado == SEMIABIERTO and self._prueba_en_curso):
                LLAMADAS_DEPENDENCIA.labels(self.dependencia, "rechazada_por_circuito").inc()
                raise CircuitoAbierto(f"Circuito hacia '{self.dependencia}' abierto")
            if estado == SEMIABIERTO:
                # Solo una llamada de prueba a la vez.
                self._prueba_en_curso = True
        try:
            resultado = operacion()
        except Exception:
            with self._candado:
                self._prueba_en_curso = False
                self._fallos += 1
                LLAMADAS_DEPENDENCIA.labels(self.dependencia, "fallo").inc()
                if self._estado == SEMIABIERTO or self._fallos >= self.umbral_fallos:
                    self._abierto_desde = self._reloj()
                    self._cambiar(ABIERTO)
            raise
        with self._candado:
            self._prueba_en_curso = False
            self._fallos = 0
            LLAMADAS_DEPENDENCIA.labels(self.dependencia, "exito").inc()
            self._cambiar(CERRADO)
        return resultado


class Backoff:
    """Esperas exponenciales con tope y jitter: 1, 2, 4, 8 … hasta `maximo` segundos."""

    def __init__(self, inicial: float = 1.0, maximo: float = 30.0, factor: float = 2.0) -> None:
        self.inicial, self.maximo, self.factor = inicial, maximo, factor
        self.intentos = 0

    def siguiente(self) -> float:
        espera = min(self.inicial * self.factor ** self.intentos, self.maximo)
        self.intentos += 1
        # ±20 %: varios hilos o Pods no reintentan todos en el mismo instante.
        return espera * random.uniform(0.8, 1.2)

    def reiniciar(self) -> None:
        self.intentos = 0


def es_error_de_base(e: BaseException) -> bool:
    """¿El fallo es de la base de datos (caída, conexión rota, pool agotado)?"""
    return isinstance(e, (OperationalError, InterfaceError, TimeoutDelPool)) or (
        isinstance(e, DBAPIError) and e.connection_invalidated)


def esperar_base_disponible(sesiones, motivo: str, detener: Callable[[], bool] = lambda: False) -> None:
    """Bloquea con backoff exponencial hasta que la base responda a un SELECT 1.

    Lo usan los hilos de fondo (relay del outbox y consumidores SQS): mientras
    la base está caída no toman trabajo nuevo, así no queman reintentos de SQS
    ni mandan a la DLQ mensajes que se habrían procesado bien un rato después.
    """
    from sqlalchemy import text

    backoff = Backoff()
    while not detener():
        try:
            with sesiones() as s:
                s.execute(text("SELECT 1"))
            if backoff.intentos:
                log.info("base_recuperada", extra={"hilo": motivo, "intentos": backoff.intentos})
            return
        except Exception as e:  # noqa: BLE001
            espera = backoff.siguiente()
            log.warning("base_no_disponible", extra={
                "hilo": motivo, "intento": backoff.intentos, "reintentoEnSegundos": round(espera, 1),
                "error": type(e).__name__})
            time.sleep(espera)
