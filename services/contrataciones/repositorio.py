"""Repositorio de Contrataciones (patrón Repository).

Lo único del camino de escritura del caso de uso —aceptar el acuerdo (que
congela la comisión), check-in y check-out— que conoce SQLAlchemy y las tablas.
Recibe y devuelve objetos del dominio (`Contratacion`, `AcuerdoTarifa`); el
patrón State (`estados.py`) y los endpoints trabajan sobre ellos sin saber qué
motor hay debajo.

No hace commit: la transacción la cierra el endpoint (Unit of Work), así el
cambio de estado y su evento en el outbox se confirman juntos.

Las consultas de solo lectura de `main.py` (listados, línea de tiempo,
resúmenes) no pasan por aquí: leen directo, como lado de consulta.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from .models import AcuerdoTarifa, Contratacion
from .outbox import registrar_evento
from .tablas import AcuerdoTarifaFila, ContratacionFila


def _columnas(modelo: BaseModel) -> dict[str, Any]:
    """Del dominio a columnas: los enums se guardan por su valor."""
    return {campo: valor.value if isinstance(valor, Enum) else valor
            for campo, valor in modelo.model_dump().items()}


class RepositorioContrataciones:
    def __init__(self, sesion: Session) -> None:
        self._s = sesion

    # --- Acuerdo de tarifa ---
    def acuerdo(self, acuerdo_id: str) -> AcuerdoTarifa | None:
        fila = self._s.get(AcuerdoTarifaFila, acuerdo_id)
        return AcuerdoTarifa.model_validate(fila) if fila else None

    def guardar_acuerdo(self, acuerdo: AcuerdoTarifa) -> None:
        fila = self._s.get(AcuerdoTarifaFila, acuerdo.id)
        for campo, valor in _columnas(acuerdo).items():
            setattr(fila, campo, valor)

    # --- Contratación ---
    def contratacion(self, contratacion_id: str) -> Contratacion | None:
        fila = self._s.get(ContratacionFila, contratacion_id)
        return Contratacion.model_validate(fila) if fila else None

    def agregar(self, contratacion: Contratacion) -> None:
        self._s.add(ContratacionFila(**_columnas(contratacion)))

    def guardar(self, contratacion: Contratacion) -> None:
        fila = self._s.get(ContratacionFila, contratacion.id)
        for campo, valor in _columnas(contratacion).items():
            setattr(fila, campo, valor)

    def registrar_evento(self, tipo: str, agregado_id: str, datos: dict[str, Any]) -> None:
        """Deja el evento en el outbox, en la misma transacción que el cambio."""
        registrar_evento(self._s, tipo, agregado_id, datos)
