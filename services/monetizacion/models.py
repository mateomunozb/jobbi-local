from __future__ import annotations

from datetime import date, datetime

from common.esquemas import Esquema


class Pago(Esquema):
    id: str
    contratacionId: str
    monto: float
    estado: str
    fechaPago: date
    referenciaPasarela: str


class SuscripcionPro(Esquema):
    id: str
    prestadorId: str
    fechaInicio: date
    fechaRenovacion: date
    estado: str
    valorMensual: float


class BilleteraPrestador(Esquema):
    id: str
    prestadorId: str
    saldoPendiente: float
    bloqueada: bool


class MovimientoBilletera(Esquema):
    id: str
    billeteraId: str
    contratacionId: str | None = None
    tipo: str
    monto: float
    fecha: date


class EventoProcesado(Esquema):
    eventoId: str
    tipo: str
    contratacionId: str | None = None
    prestadorId: str | None = None
    monto: float
    resultado: str
    origen: str
    mensajeSqsId: str | None = None
    ocurridoEn: datetime | None = None
    fechaProcesado: datetime
    latenciaMs: float | None = None
