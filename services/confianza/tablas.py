"""Tablas del contexto Confianza y Verificación (base `jobbi_confianza`)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
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


class SujetoVerificacionFila(Base):
    """Estado de verificación de cada prestador (RN-01): la fuente de verdad.

    Nace PENDIENTE al registrarse el prestador y pasa a APROBADA o RECHAZADA
    cuando el aliado responde. Si el aliado está caído queda PENDIENTE, y el
    reverificador de fondo lo reintenta. El veredicto vale
    VERIFICACION_VIGENCIA_DIAS: después se renueva, conservando el anterior
    mientras tanto.

    Guarda el número de documento porque sin él no se puede consultar al
    aliado (ni al registrarse ni al renovar). Es dato de este contexto y nunca
    se escribe en los logs.
    """

    __tablename__ = "sujetos_verificacion"

    # Id del perfil verificado. Conserva el nombre histórico, pero desde que el
    # demandante también se verifica (RN-01) puede ser de cualquiera de los dos:
    # `tipo` dice cuál, y así a qué perfil de Identidad se le publica el veredicto.
    prestadorId: Mapped[str] = mapped_column(String(36), primary_key=True)
    tipo: Mapped[str] = mapped_column(String(20), index=True, server_default="PRESTADOR")  # PRESTADOR | DEMANDANTE
    documento: Mapped[str] = mapped_column(String(30))
    estado: Mapped[str] = mapped_column(String(20), index=True)  # PENDIENTE | APROBADA | RECHAZADA
    fechaVeredicto: Mapped[date | None] = mapped_column(Date, nullable=True)
    # ¿Identidad ya muestra este estado? Si no, el reverificador se lo vuelve a enviar.
    sincronizado: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    ultimoIntento: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    creadoEn: Mapped[datetime] = mapped_column(DateTime, index=True)
