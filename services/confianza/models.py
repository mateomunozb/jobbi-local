from __future__ import annotations

from datetime import date

from pydantic import Field

from common.esquemas import Esquema

from common.enums import EstadoModeracion, EstadoVerificacion


class AliadoVerificacion(Esquema):
    id: str
    nombre: str
    costoPorConsulta: float
    slaHoras: int


class VerificacionIdentidad(Esquema):
    id: str
    prestadorId: str
    aliadoVerificacionId: str
    tipoVerificacion: str
    estado: EstadoVerificacion
    fechaSolicitud: date
    fechaResultado: date | None = None
    resultadoDetalle: str
    costo: float


class Resena(Esquema):
    id: str
    contratacionId: str
    autorId: str
    receptorId: str
    puntuacion: int = Field(ge=1, le=5)
    comentario: str
    fecha: date
    estadoModeracion: EstadoModeracion
