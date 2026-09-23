"""Tablas del contexto Adquisición y Distribución (base `jobbi_adquisicion`)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


class AliadoDistribucionFila(Base):
    __tablename__ = "aliados_distribucion"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120))
    tipo: Mapped[str] = mapped_column(String(40), index=True)
    zonaCobertura: Mapped[str] = mapped_column(String(120), index=True)


class ReferidoFila(Base):
    __tablename__ = "referidos"

    aliadoId: Mapped[str] = mapped_column(
        ForeignKey("aliados_distribucion.id"), primary_key=True, index=True)
    # El prestador referido vive en el contexto Identidad: solo su UUID.
    prestadorId: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    fechaReferencia: Mapped[date] = mapped_column(Date, index=True)
    activado: Mapped[bool] = mapped_column(Boolean, index=True)
