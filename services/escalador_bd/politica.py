"""Política de auto-escalado de la base: decide si subir, bajar o mantener la capacidad.

No hace E/S: recibe el uso medido y responde qué hacer, así se prueba sola.

Capacidad en ACU, como Aurora Serverless v2 (que escala en pasos desde 0,5
ACU). Aquí 1 ACU equivale a 1 núcleo y ~1 GiB, a escala del clúster local; cada
nivel dobla al anterior. El nivel más bajo es la capacidad normal de la base.

Reglas:

- **Subir** un nivel si la CPU usada pasa del `umbral_subida` (75 %) de la
  asignada durante `muestras_subida` lecturas seguidas (10 s), o si la memoria
  pasa del 85 % de la asignada. Espera `enfriamiento_subida` (15 s) entre
  subidas para ver el efecto de la anterior.
- **Bajar** un nivel si lo que se usa cabría holgado en el nivel inferior (CPU
  por debajo del `umbral_bajada`, 50 %, de la del nivel inferior, y memoria por
  debajo del 80 % de la suya) de forma sostenida durante `segundos_bajada`
  (30 s). Bajar con holgura evita el vaivén: si al bajar quedara justo, al
  siguiente instante volvería a subir.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Nivel:
    acu: float
    cpu_limite_m: int
    cpu_reserva_m: int
    memoria_limite_mi: int
    memoria_reserva_mi: int
    work_mem_mb: int

    @property
    def cpu_nucleos(self) -> float:
        return self.cpu_limite_m / 1000

    @property
    def memoria_bytes(self) -> int:
        return self.memoria_limite_mi * 2**20


# El primero coincide con los recursos de k8s/postgres-deployment.yaml: es a
# donde vuelve la base cuando pasa la saturación (y tras reiniciar el Pod).
NIVELES = (
    Nivel(0.5, 500, 100, 640, 256, 4),
    Nivel(1, 1000, 250, 1024, 384, 8),
    Nivel(2, 2000, 500, 1536, 512, 16),
    Nivel(4, 4000, 1000, 2048, 768, 32),
)

SUBIR, BAJAR, MANTENER = "SUBIR", "BAJAR", "MANTENER"


class Politica:
    def __init__(self, umbral_subida: float = 0.75, umbral_bajada: float = 0.50,
                 memoria_subida: float = 0.85, memoria_bajada: float = 0.80,
                 muestras_subida: int = 2, enfriamiento_subida: float = 15.0,
                 segundos_bajada: float = 30.0, reloj: Callable[[], float] = time.monotonic) -> None:
        self.umbral_subida = umbral_subida
        self.umbral_bajada = umbral_bajada
        self.memoria_subida = memoria_subida
        self.memoria_bajada = memoria_bajada
        self.muestras_subida = muestras_subida
        self.enfriamiento_subida = enfriamiento_subida
        self.segundos_bajada = segundos_bajada
        self._reloj = reloj
        self._altas_seguidas = 0
        self._baja_desde: float | None = None
        self._ultimo_cambio = float("-inf")

    def cambio_aplicado(self) -> None:
        """Reinicia las ventanas: la siguiente decisión mira el uso con la capacidad nueva."""
        self._ultimo_cambio = self._reloj()
        self._altas_seguidas = 0
        self._baja_desde = None

    def decidir(self, indice: int, cpu_nucleos: float, memoria_bytes: float) -> tuple[str, str]:
        """Devuelve (SUBIR | BAJAR | MANTENER, motivo)."""
        ahora = self._reloj()
        actual = NIVELES[indice]
        cpu_pct = cpu_nucleos / actual.cpu_nucleos
        mem_pct = memoria_bytes / actual.memoria_bytes

        alta = cpu_pct >= self.umbral_subida or mem_pct >= self.memoria_subida
        self._altas_seguidas = self._altas_seguidas + 1 if alta else 0
        if alta:
            self._baja_desde = None
            if indice == len(NIVELES) - 1:
                return MANTENER, "saturada en la capacidad máxima"
            if self._altas_seguidas < self.muestras_subida:
                return MANTENER, "alta, confirmando"
            if ahora - self._ultimo_cambio < self.enfriamiento_subida:
                return MANTENER, "alta, esperando el efecto de la subida anterior"
            motivo = (f"CPU al {cpu_pct:.0%} de la asignada" if cpu_pct >= self.umbral_subida
                      else f"memoria al {mem_pct:.0%} de la asignada")
            return SUBIR, motivo

        if indice == 0:
            self._baja_desde = None
            return MANTENER, "capacidad mínima"
        inferior = NIVELES[indice - 1]
        cabe = (cpu_nucleos / inferior.cpu_nucleos <= self.umbral_bajada
                and memoria_bytes / inferior.memoria_bytes <= self.memoria_bajada)
        if not cabe:
            self._baja_desde = None
            return MANTENER, "el uso no cabe holgado en el nivel inferior"
        if self._baja_desde is None:
            self._baja_desde = ahora
        if ahora - self._baja_desde < self.segundos_bajada:
            return MANTENER, "baja, confirmando que la saturación pasó"
        return BAJAR, f"CPU al {cpu_nucleos / inferior.cpu_nucleos:.0%} de la del nivel inferior"
