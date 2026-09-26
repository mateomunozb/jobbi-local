from __future__ import annotations

from datetime import date, datetime

from common.esquemas import Esquema


class Conversacion(Esquema):
    id: str
    contactoId: str
    fechaInicio: date
    estado: str = "ABIERTA"
    creadaEn: datetime | None = None
    fechaCierre: datetime | None = None
    contratacionId: str | None = None


class Mensaje(Esquema):
    id: str
    conversacionId: str
    remitenteId: str
    contenido: str
    fechaEnvio: datetime
    leido: bool


class Notificacion(Esquema):
    id: str
    usuarioId: str
    tipo: str
    canal: str
    contenido: str
    fechaEnvio: datetime
    leida: bool
