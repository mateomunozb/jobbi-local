"""Tablas del contexto Comunicación (base `jobbi_comunicacion`)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


class ConversacionFila(Base):
    __tablename__ = "conversaciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # El contacto vive en el contexto Mercado: solo se guarda su UUID.
    contactoId: Mapped[str] = mapped_column(String(36), index=True)
    fechaInicio: Mapped[date] = mapped_column(Date)


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
