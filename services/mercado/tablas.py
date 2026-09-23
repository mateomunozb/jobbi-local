"""Tablas del contexto Mercado de Oficios (base `jobbi_mercado`)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


class CategoriaFila(Base):
    __tablename__ = "categorias"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(80))
    descripcion: Mapped[str] = mapped_column(String(300))


class OficioFila(Base):
    __tablename__ = "oficios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    categoriaId: Mapped[str] = mapped_column(ForeignKey("categorias.id"), index=True)
    nombre: Mapped[str] = mapped_column(String(120))
    descripcion: Mapped[str] = mapped_column(String(300))


class PrestadorOficioFila(Base):
    """Asociación prestador↔oficio.

    `prestadorId` apunta a un perfil del contexto Identidad, que vive en otra
    base: por eso es un String sin ForeignKey. La integridad entre contextos no
    la garantiza el motor, sino el flujo de la aplicación.
    """

    __tablename__ = "prestador_oficios"

    prestadorId: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)
    oficioId: Mapped[str] = mapped_column(ForeignKey("oficios.id"), primary_key=True, index=True)
    tarifaReferencial: Mapped[float] = mapped_column(Float)
    anosExperiencia: Mapped[int] = mapped_column(Integer)


class BusquedaFila(Base):
    __tablename__ = "busquedas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    demandanteId: Mapped[str] = mapped_column(String(36), index=True)
    categoriaId: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    calificacionMinima: Mapped[float | None] = mapped_column(Float, nullable=True)
    fecha: Mapped[date] = mapped_column(Date, index=True)

    municipio: Mapped[str] = mapped_column(String(80), index=True)
    comuna: Mapped[str | None] = mapped_column(String(80), nullable=True)
    barrio: Mapped[str | None] = mapped_column(String(80), nullable=True)
    latitud: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitud: Mapped[float | None] = mapped_column(Float, nullable=True)

    @property
    def ubicacionFiltro(self) -> dict:
        return {
            "municipio": self.municipio, "comuna": self.comuna, "barrio": self.barrio,
            "latitud": self.latitud, "longitud": self.longitud,
        }


class ContactoFila(Base):
    __tablename__ = "contactos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    demandanteId: Mapped[str] = mapped_column(String(36), index=True)
    prestadorId: Mapped[str] = mapped_column(String(36), index=True)
    busquedaOrigenId: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    fechaInicio: Mapped[date] = mapped_column(Date, index=True)
    estado: Mapped[str] = mapped_column(String(20), index=True)
