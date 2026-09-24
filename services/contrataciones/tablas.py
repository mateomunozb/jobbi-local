"""Tablas del contexto Contrataciones (base `jobbi_contrataciones`)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint
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


class EventoOutboxFila(Base):
    """Bandeja de salida del patrón Transactional Outbox.

    Un evento se escribe aquí en la **misma transacción** que el cambio de
    negocio que lo origina (el check-out). Así nunca existe una contratación
    completada sin su evento, ni un evento de una contratación que no se
    completó. Publicarlo en SNS es trabajo aparte del relay: si LocalStack está
    caído, el evento espera aquí en PENDIENTE y sale cuando vuelva.
    """

    __tablename__ = "outbox_eventos"
    # Un agregado emite cada tipo de evento una sola vez: dos check-out
    # simultáneos de la misma contratación no pueden dejar dos eventos.
    __table_args__ = (UniqueConstraint("tipo", "agregadoId", name="uq_outbox_tipo_agregado"),)

    # Es también el eventoId que viaja en el mensaje: el consumidor lo usa para
    # descartar duplicados.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tipo: Mapped[str] = mapped_column(String(60), index=True)
    agregadoId: Mapped[str] = mapped_column(String(36), index=True)
    payload: Mapped[str] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(String(20), index=True)  # PENDIENTE | PUBLICADO
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    ultimoError: Mapped[str | None] = mapped_column(Text, nullable=True)
    fechaCreacion: Mapped[datetime] = mapped_column(DateTime, index=True)
    fechaPublicacion: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mensajeSnsId: Mapped[str | None] = mapped_column(String(100), nullable=True)
