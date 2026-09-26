from __future__ import annotations

from datetime import date

from pydantic import Field

from common.esquemas import Esquema

from common.enums import EstadoCuenta, EstadoVerificacion, PlanPrestador


class Ubicacion(Esquema):
    """Objeto de valor: no tiene identidad propia, viaja embebido en el perfil."""

    municipio: str
    comuna: str
    barrio: str
    latitud: float
    longitud: float


class Usuario(Esquema):
    id: str
    nombreCompleto: str
    correo: str
    telefono: str
    tipoDocumento: str
    numeroDocumento: str
    fechaRegistro: date
    estado: EstadoCuenta


class PerfilDemandante(Esquema):
    id: str
    usuarioId: str
    nombreCompleto: str = ""
    ubicacionPrincipal: Ubicacion
    fechaActivacion: date
    # RN-01: el demandante también pasa por el aliado de verificación.
    estadoVerificacionActual: EstadoVerificacion
    insigniaVerificado: bool


class PerfilPrestador(Esquema):
    """Perfil público del prestador.

    Incluye el nombre y el teléfono porque `Usuario` vive en este mismo
    contexto: unirlos aquí no cruza ninguna frontera, y evita que cada
    consumidor tenga que pedir el usuario aparte por cada tarjeta que pinta.
    """

    id: str
    usuarioId: str
    nombreCompleto: str = ""
    telefono: str = ""
    descripcion: str
    portafolioUrl: str
    tarifaReferencialBase: float
    insigniaVerificado: bool
    estadoVerificacionActual: EstadoVerificacion
    planActual: PlanPrestador
    calificacionPromedio: float = Field(ge=0, le=5)
    totalResenas: int
    fechaActivacion: date
    ubicacionPrincipal: Ubicacion
