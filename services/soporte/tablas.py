"""Tablas del contexto Soporte y Disputas (base `jobbi_soporte`)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


class IncidenteFila(Base):
    __tablename__ = "incidentes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # La contratación vive en otro contexto: solo se guarda su UUID.
    contratacionId: Mapped[str] = mapped_column(String(36), index=True)
    tipo: Mapped[str] = mapped_column(String(40), index=True)
    estado: Mapped[str] = mapped_column(String(20), index=True)
    evidenciaDescripcion: Mapped[str] = mapped_column(Text)
    resolucion: Mapped[str | None] = mapped_column(Text, nullable=True)
    fechaApertura: Mapped[date] = mapped_column(Date, index=True)
    fechaCierre: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
