"""Tablas del contexto Protección / Seguros (base `jobbi_proteccion`)."""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


class AseguradoraFila(Base):
    __tablename__ = "aseguradoras"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120))
    vigiladoPor: Mapped[str] = mapped_column(String(120))


class PlanProteccionFila(Base):
    __tablename__ = "planes_proteccion"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    aseguradoraId: Mapped[str] = mapped_column(ForeignKey("aseguradoras.id"), index=True)
    contratacionId: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    prima: Mapped[float] = mapped_column(Float)
    cobertura: Mapped[str] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(String(20), index=True)
