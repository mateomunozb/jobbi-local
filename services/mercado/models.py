from __future__ import annotations

from datetime import date

from common.esquemas import Esquema


class UbicacionFiltro(Esquema):
    municipio: str
    comuna: str | None = None
    barrio: str | None = None
    latitud: float | None = None
    longitud: float | None = None


class Categoria(Esquema):
    id: str
    nombre: str
    descripcion: str


class Oficio(Esquema):
    id: str
    categoriaId: str
    nombre: str
    descripcion: str


class PrestadorOficio(Esquema):
    """Objeto de valor: la asociación prestador↔oficio con su tarifa."""

    prestadorId: str
    oficioId: str
    tarifaReferencial: float
    anosExperiencia: int


class Busqueda(Esquema):
    """Objeto de valor con identidad técnica para poder trazar el origen del contacto."""

    id: str
    demandanteId: str
    categoriaId: str | None = None
    ubicacionFiltro: UbicacionFiltro
    calificacionMinima: float | None = None
    fecha: date


class Contacto(Esquema):
    id: str
    demandanteId: str
    prestadorId: str
    busquedaOrigenId: str | None = None
    fechaInicio: date
    estado: str
