"""Tablas del contexto Comunicación (base `jobbi_comunicacion`)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


class ConversacionFila(Base):
    """Un chat por servicio.

    Un contacto (la pareja demandante–prestador) puede tener varias
    conversaciones a lo largo del tiempo, pero solo una ABIERTA: cuando el
    servicio que se acordó en ella se cierra, el chat queda CERRADO (solo
    lectura) y volver a contactar abre uno nuevo.
    """

    __tablename__ = "conversaciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # El contacto vive en el contexto Mercado: solo se guarda su UUID.
    contactoId: Mapped[str] = mapped_column(String(36), index=True)
    fechaInicio: Mapped[date] = mapped_column(Date)
    # Columnas añadidas con el chat por servicio: anulables o con valor por
    # defecto, para que las bases existentes las ganen sin perder datos.
    estado: Mapped[str] = mapped_column(String(20), server_default="ABIERTA", default="ABIERTA", index=True)
    creadaEn: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fechaCierre: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # El servicio con el que se cerró (Contrataciones): solo el UUID.
    contratacionId: Mapped[str | None] = mapped_column(String(36), nullable=True)


class MensajeFila(Base):
    __tablename__ = "mensajes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversacionId: Mapped[str] = mapped_column(ForeignKey("conversaciones.id"), index=True)
    remitenteId: Mapped[str] = mapped_column(String(36), index=True)
    contenido: Mapped[str] = mapped_column(Text)
    fechaEnvio: Mapped[datetime] = mapped_column(DateTime, index=True)
    leido: Mapped[bool] = mapped_column(Boolean, index=True)


class NotificacionFila(Base):
    __tablename__ = "notificaciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    usuarioId: Mapped[str] = mapped_column(String(36), index=True)
    tipo: Mapped[str] = mapped_column(String(40), index=True)
    canal: Mapped[str] = mapped_column(String(20), index=True)
    contenido: Mapped[str] = mapped_column(Text)
    fechaEnvio: Mapped[datetime] = mapped_column(DateTime, index=True)
    leida: Mapped[bool] = mapped_column(Boolean, index=True)


class EventoRecibidoFila(Base):
    """Bandeja de entrada del consumidor de Comunicación (Idempotent Receiver).

    SNS/SQS entregan *al menos una vez*. Registrar cada `eventoId` en la misma
    transacción que la notificación hace que un duplicado choque con la clave
    primaria y el prestador no reciba dos veces el mismo aviso.
    """

    __tablename__ = "eventos_recibidos"

    eventoId: Mapped[str] = mapped_column(String(100), primary_key=True)
    tipo: Mapped[str] = mapped_column(String(60), index=True)
    # NOTIFICADO | IGNORADO
    resultado: Mapped[str] = mapped_column(String(40), index=True)
    notificacionId: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mensajeSqsId: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fechaProcesado: Mapped[datetime] = mapped_column(DateTime, index=True)
