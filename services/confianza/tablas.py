"""Tablas del contexto Confianza y Verificación (base `jobbi_confianza`)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


class AliadoVerificacionFila(Base):
    __tablename__ = "aliados_verificacion"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120))
    costoPorConsulta: Mapped[float] = mapped_column(Float)
    slaHoras: Mapped[int] = mapped_column(Integer, index=True)


class VerificacionIdentidadFila(Base):
    __tablename__ = "verificaciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    prestadorId: Mapped[str] = mapped_column(String(36), index=True)
    aliadoVerificacionId: Mapped[str] = mapped_column(
        ForeignKey("aliados_verificacion.id"), index=True)
    tipoVerificacion: Mapped[str] = mapped_column(String(30), index=True)
    estado: Mapped[str] = mapped_column(String(20), index=True)
    fechaSolicitud: Mapped[date] = mapped_column(Date, index=True)
    fechaResultado: Mapped[date | None] = mapped_column(Date, nullable=True)
    resultadoDetalle: Mapped[str] = mapped_column(Text)
    costo: Mapped[float] = mapped_column(Float)


class ResenaFila(Base):
    __tablename__ = "resenas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contratacionId: Mapped[str] = mapped_column(String(36), index=True)
    autorId: Mapped[str] = mapped_column(String(36), index=True)
    receptorId: Mapped[str] = mapped_column(String(36), index=True)
    puntuacion: Mapped[int] = mapped_column(Integer, index=True)
    comentario: Mapped[str] = mapped_column(Text)
    fecha: Mapped[date] = mapped_column(Date, index=True)
    estadoModeracion: Mapped[str] = mapped_column(String(20), index=True)
