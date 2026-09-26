"""Tablas del contexto Monetización (base `jobbi_monetizacion`)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
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


class EventoProcesadoFila(Base):
    """Bandeja de entrada del consumidor (patrón Idempotent Consumer).

    SQS entrega *al menos una vez*: el mismo mensaje puede llegar dos veces, y
    el outbox del productor también puede publicarlo dos veces. Registrar aquí
    cada `eventoId` en la misma transacción que el cobro hace que el duplicado
    choque con la clave primaria y se descarte sin tocar la billetera.

    Es además el historial persistente de lo que procesó el worker (antes vivía
    en una lista en memoria que se perdía al reiniciar el Pod).
    """

    __tablename__ = "eventos_procesados"

    eventoId: Mapped[str] = mapped_column(String(100), primary_key=True)
    tipo: Mapped[str] = mapped_column(String(60), index=True)
    contratacionId: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    prestadorId: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    monto: Mapped[float] = mapped_column(Float, default=0.0)
    # COMISION_COBRADA | YA_COBRADA | SIN_COBRO_MEDIO_PLATAFORMA |
    # REGISTRADO_SIN_CONTRATACION (y COBRO_SINCRONO en registros históricos)
    resultado: Mapped[str] = mapped_column(String(40), index=True)
    origen: Mapped[str] = mapped_column(String(20))  # ASINCRONO_SQS (SINCRONO solo en registros históricos)
    mensajeSqsId: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ocurridoEn: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fechaProcesado: Mapped[datetime] = mapped_column(DateTime, index=True)
    # Desde que ocurrió el hecho (check-out) hasta que quedó cobrado.
    latenciaMs: Mapped[float | None] = mapped_column(Float, nullable=True)


class EventoOutboxMonetizacionFila(Base):
    """Bandeja de salida de Monetización (Transactional Outbox).

    El evento BILLETERA_BLOQUEADA se escribe aquí en la misma transacción que
    el cargo que hizo cruzar el umbral: no hay billetera bloqueada sin su aviso,
    ni aviso de un bloqueo que no se guardó. `common/outbox.py` lo publica.

    Nombre propio de tabla porque todas las tablas comparten metadatos y
    Contrataciones ya usa `outbox_eventos`.
    """

    __tablename__ = "outbox_monetizacion"

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
