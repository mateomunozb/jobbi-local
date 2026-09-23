"""Tablas del contexto Contrataciones (base `jobbi_contrataciones`)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


class ContratacionFila(Base):
    """Los ids de demandante, prestador y oficio apuntan a otros contextos
    (otras bases), así que son String sin ForeignKey."""

    __tablename__ = "contrataciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    demandanteId: Mapped[str] = mapped_column(String(36), index=True)
    prestadorId: Mapped[str] = mapped_column(String(36), index=True)
    oficioId: Mapped[str] = mapped_column(String(36), index=True)
    fechaSolicitud: Mapped[date] = mapped_column(Date, index=True)
    fechaEjecucion: Mapped[date | None] = mapped_column(Date, nullable=True)
    estado: Mapped[str] = mapped_column(String(20), index=True)
    valorAcordado: Mapped[float] = mapped_column(Float)
    medioPago: Mapped[str] = mapped_column(String(20), index=True)
    porcentajeComisionAplicado: Mapped[float] = mapped_column(Float)
    montoComision: Mapped[float] = mapped_column(Float)
    checkIn: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checkOut: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AcuerdoTarifaFila(Base):
    """Negociación de la tarifa dentro del chat, previa a la contratación.

    El servicio no nace del chat sin más: alguien propone un valor y la otra
    parte lo acepta. Solo cuando ambas aceptaron se crea la Contratación, y es
    en ese instante cuando el porcentaje de comisión queda congelado: el trato
    se hizo bajo el plan que el prestador tenía entonces, y cambiarse de plan
    después no reescribe lo acordado.
    """

    __tablename__ = "acuerdos_tarifa"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # El contacto vive en Mercado y la conversación en Comunicación: solo UUID.
    contactoId: Mapped[str] = mapped_column(String(36), index=True)
    conversacionId: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    demandanteId: Mapped[str] = mapped_column(String(36), index=True)
    prestadorId: Mapped[str] = mapped_column(String(36), index=True)
    oficioId: Mapped[str] = mapped_column(String(36), index=True)
    valorPropuesto: Mapped[float] = mapped_column(Float)
    medioPago: Mapped[str] = mapped_column(String(20))
    propuestoPor: Mapped[str] = mapped_column(String(20))
    aceptadoDemandante: Mapped[bool] = mapped_column(Boolean, default=False)
    aceptadoPrestador: Mapped[bool] = mapped_column(Boolean, default=False)
    estado: Mapped[str] = mapped_column(String(20), index=True)
    # Nulo mientras el acuerdo sigue abierto; se fija al cerrarse.
    porcentajeComisionCongelado: Mapped[float | None] = mapped_column(Float, nullable=True)
    planPrestadorAlAcordar: Mapped[str | None] = mapped_column(String(10), nullable=True)
    contratacionId: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    fechaPropuesta: Mapped[datetime] = mapped_column(DateTime, index=True)
    fechaCierre: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
