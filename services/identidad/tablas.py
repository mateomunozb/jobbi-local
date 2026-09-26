"""Tablas del contexto Identidad (base `jobbi_identidad`).

La `Ubicacion` del modelo de dominio es un objeto de valor: no tiene identidad
propia ni ciclo de vida separado, así que se guarda embebida en columnas del
perfil en vez de en su propia tabla.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base


class UsuarioFila(Base):
    __tablename__ = "usuarios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nombreCompleto: Mapped[str] = mapped_column(String(120))
    # El correo identifica la cuenta en el login, así que se fuerza único.
    correo: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    telefono: Mapped[str] = mapped_column(String(30))
    tipoDocumento: Mapped[str] = mapped_column(String(10))
    numeroDocumento: Mapped[str] = mapped_column(String(30))
    fechaRegistro: Mapped[date] = mapped_column(Date)
    estado: Mapped[str] = mapped_column(String(20), index=True)


class PerfilDemandanteFila(Base):
    __tablename__ = "perfiles_demandante"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    usuarioId: Mapped[str] = mapped_column(ForeignKey("usuarios.id"), index=True)
    nombreCompleto: Mapped[str] = mapped_column(String(120), default="")
    fechaActivacion: Mapped[date] = mapped_column(Date)
    # El demandante también se verifica con el aliado (RN-01). El valor por
    # defecto solo lo toman los perfiles que ya existían al agregar la columna:
    # se dan por aprobados para no romper las cuentas creadas antes de la regla.
    # Uno nuevo nace PENDIENTE (auth.py).
    estadoVerificacionActual: Mapped[str] = mapped_column(String(20), index=True, server_default="APROBADA")

    municipio: Mapped[str] = mapped_column(String(80), index=True)
    comuna: Mapped[str] = mapped_column(String(80))
    barrio: Mapped[str] = mapped_column(String(80))
    latitud: Mapped[float] = mapped_column(Float)
    longitud: Mapped[float] = mapped_column(Float)

    @property
    def insigniaVerificado(self) -> bool:
        return self.estadoVerificacionActual == "APROBADA"


class PerfilPrestadorFila(Base):
    __tablename__ = "perfiles_prestador"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    usuarioId: Mapped[str] = mapped_column(ForeignKey("usuarios.id"), index=True)
    nombreCompleto: Mapped[str] = mapped_column(String(120), default="")
    telefono: Mapped[str] = mapped_column(String(30), default="")
    descripcion: Mapped[str] = mapped_column(String(500))
    portafolioUrl: Mapped[str] = mapped_column(String(300), default="")
    tarifaReferencialBase: Mapped[float] = mapped_column(Float)
    insigniaVerificado: Mapped[bool] = mapped_column(Boolean, index=True)
    estadoVerificacionActual: Mapped[str] = mapped_column(String(20), index=True)
    planActual: Mapped[str] = mapped_column(String(10), index=True)
    calificacionPromedio: Mapped[float] = mapped_column(Float, index=True)
    totalResenas: Mapped[int] = mapped_column(Integer)
    fechaActivacion: Mapped[date] = mapped_column(Date)

    municipio: Mapped[str] = mapped_column(String(80), index=True)
    comuna: Mapped[str] = mapped_column(String(80))
    barrio: Mapped[str] = mapped_column(String(80))
    latitud: Mapped[float] = mapped_column(Float)
    longitud: Mapped[float] = mapped_column(Float)

    @property
    def ubicacionPrincipal(self) -> dict:
        """Recompone el objeto de valor para el esquema de respuesta."""
        return {
            "municipio": self.municipio, "comuna": self.comuna, "barrio": self.barrio,
            "latitud": self.latitud, "longitud": self.longitud,
        }


# Mismo accesorio para el perfil de demandante; se define aparte para no
# heredar entre tablas y mantener cada una explícita.
PerfilDemandanteFila.ubicacionPrincipal = property(  # type: ignore[attr-defined]
    lambda self: {
        "municipio": self.municipio, "comuna": self.comuna, "barrio": self.barrio,
        "latitud": self.latitud, "longitud": self.longitud,
    }
)
