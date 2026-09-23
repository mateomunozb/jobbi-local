"""Tablas del contexto Monetización (base `jobbi_monetizacion`)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


class PagoFila(Base):
    __tablename__ = "pagos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contratacionId: Mapped[str] = mapped_column(String(36), index=True)
    monto: Mapped[float] = mapped_column(Float)
    estado: Mapped[str] = mapped_column(String(20), index=True)
    fechaPago: Mapped[date] = mapped_column(Date, index=True)
    referenciaPasarela: Mapped[str] = mapped_column(String(60))


class SuscripcionProFila(Base):
    __tablename__ = "suscripciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    prestadorId: Mapped[str] = mapped_column(String(36), index=True)
    fechaInicio: Mapped[date] = mapped_column(Date, index=True)
    fechaRenovacion: Mapped[date] = mapped_column(Date)
    estado: Mapped[str] = mapped_column(String(20), index=True)
    valorMensual: Mapped[float] = mapped_column(Float)


class BilleteraPrestadorFila(Base):
    __tablename__ = "billeteras"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    prestadorId: Mapped[str] = mapped_column(String(36), index=True, unique=True)
    saldoPendiente: Mapped[float] = mapped_column(Float)
    bloqueada: Mapped[bool] = mapped_column(Boolean, index=True)


class MovimientoBilleteraFila(Base):
    __tablename__ = "movimientos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    billeteraId: Mapped[str] = mapped_column(ForeignKey("billeteras.id"), index=True)
    contratacionId: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    tipo: Mapped[str] = mapped_column(String(30), index=True)
    monto: Mapped[float] = mapped_column(Float)
    fecha: Mapped[date] = mapped_column(Date, index=True)
