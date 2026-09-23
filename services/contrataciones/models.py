from __future__ import annotations

from datetime import date, datetime

from common.esquemas import Esquema

from common.enums import EstadoContratacion, MedioPago


class AcuerdoTarifa(Esquema):
    id: str
    contactoId: str
    conversacionId: str | None = None
    demandanteId: str
    prestadorId: str
    oficioId: str
    valorPropuesto: float
    medioPago: MedioPago
    propuestoPor: str
    aceptadoDemandante: bool
    aceptadoPrestador: bool
    estado: str
    porcentajeComisionCongelado: float | None = None
    planPrestadorAlAcordar: str | None = None
    contratacionId: str | None = None
    fechaPropuesta: datetime
    fechaCierre: datetime | None = None


class Contratacion(Esquema):
    id: str
    demandanteId: str
    prestadorId: str
    oficioId: str
    fechaSolicitud: date
    fechaEjecucion: date | None = None
    estado: EstadoContratacion
    valorAcordado: float
    medioPago: MedioPago
    porcentajeComisionAplicado: float
    montoComision: float
    checkIn: datetime | None = None
    checkOut: datetime | None = None
