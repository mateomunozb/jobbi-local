"""Contexto delimitado: Mercado de Oficios.

Dueño del catálogo (Categoría, Oficio), de la oferta de cada prestador
(PrestadorOficio) y de la demanda expresada (Búsqueda, Contacto), en la base
`jobbi_mercado`. No conoce el nombre ni el correo de las personas: solo sus UUID.
"""

from __future__ import annotations

from datetime import date

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from common.db import condiciones, inicializar, listar, nuevo_id, paginar_consulta
from common.observabilidad import evento_negocio, registrar_metricas
from common.service import crear_servicio

from .models import Busqueda, Categoria, Contacto, Oficio, PrestadorOficio
from .tablas import BusquedaFila, CategoriaFila, ContactoFila, OficioFila, PrestadorOficioFila

app = crear_servicio(
    nombre="mercado",
    contexto="Mercado de Oficios",
    descripcion=(
        "Catálogo de categorías y oficios, la oferta de cada prestador y la "
        "demanda expresada mediante búsquedas y contactos."
    ),
)


Sesion: sessionmaker[Session] = inicializar("mercado")


def _metricas_de_negocio():
    """Match rate = contactos iniciados / búsquedas realizadas (se divide en Prometheus)."""
    with Sesion() as s:
        busquedas = s.scalar(select(func.count()).select_from(BusquedaFila)) or 0
        # Solo los contactos que nacieron de una búsqueda: los que se abren
        # desde el perfil o un referido no miden la conversión de la búsqueda.
        contactos = s.scalar(select(func.count()).where(ContactoFila.busquedaOrigenId.is_not(None))) or 0
    return [
        ("jobbi_busquedas_registradas", "Búsquedas realizadas por demandantes", {}, busquedas),
        ("jobbi_contactos_iniciados", "Contactos iniciados a partir de una búsqueda", {}, contactos),
    ]


registrar_metricas(_metricas_de_negocio)


def sesion() -> Session:
    with Sesion() as s:
        yield s


# --- Escrituras ------------------------------------------------------------
# El catálogo no viene sembrado: se construye con lo que declaran los
# prestadores al registrarse. Por eso el alta de un oficio crea la categoría y
# el oficio si aún no existen, en vez de exigir que alguien los haya cargado.

class AltaOficio(BaseModel):
    categoriaNombre: str = Field(min_length=2, max_length=80)
    oficioNombre: str = Field(min_length=2, max_length=120)
    descripcion: str | None = None


class AltaOferta(BaseModel):
    prestadorId: str
    oficioId: str
    tarifaReferencial: float = Field(ge=0)
    anosExperiencia: int = Field(default=0, ge=0, le=70)


class AltaContacto(BaseModel):
    demandanteId: str
    prestadorId: str
    busquedaOrigenId: str | None = None


class AltaBusqueda(BaseModel):
    demandanteId: str
    categoriaId: str | None = None
    calificacionMinima: float | None = Field(default=None, ge=0, le=5)
    municipio: str = Field(default="Medellín", max_length=80)
    comuna: str | None = None
    barrio: str | None = None
    latitud: float | None = None
    longitud: float | None = None


@app.post("/busquedas", tags=["demanda"], status_code=201, response_model=Busqueda,
          summary="Registrar una búsqueda del demandante")
def alta_busqueda(peticion: AltaBusqueda, s: Session = Depends(sesion)):
    """Deja constancia de que un demandante buscó, con los filtros que usó.

    Es el denominador de la tasa de contacto tras búsqueda: el contacto que
    nazca de estos resultados la cita en `busquedaOrigenId`.
    """
    busqueda = BusquedaFila(id=nuevo_id(), fecha=date.today(), **peticion.model_dump())
    s.add(busqueda)
    s.commit()
    evento_negocio("busqueda_registrada", "El demandante ejecutó una búsqueda",
                   busquedaId=busqueda.id, demandanteId=busqueda.demandanteId,
                   categoriaId=busqueda.categoriaId)
    return Busqueda.model_validate(busqueda)


@app.post("/catalogo/oficio", tags=["catálogo"], status_code=201,
          summary="Registrar un oficio, creando su categoría si hace falta")
def alta_oficio(peticion: AltaOficio, s: Session = Depends(sesion)):
    categoria = s.scalars(select(CategoriaFila).where(
        func.lower(CategoriaFila.nombre) == peticion.categoriaNombre.strip().lower()
    )).first()
    if categoria is None:
        categoria = CategoriaFila(
            id=nuevo_id(), nombre=peticion.categoriaNombre.strip(),
            descripcion=f"Servicios de {peticion.categoriaNombre.strip().lower()}.",
        )
        s.add(categoria)
        s.flush()  # el oficio referencia la categoría por clave foránea

    oficio = s.scalars(select(OficioFila).where(
        OficioFila.categoriaId == categoria.id,
        func.lower(OficioFila.nombre) == peticion.oficioNombre.strip().lower(),
    )).first()
    if oficio is None:
        oficio = OficioFila(
            id=nuevo_id(), categoriaId=categoria.id, nombre=peticion.oficioNombre.strip(),
            descripcion=peticion.descripcion or peticion.oficioNombre.strip(),
        )
        s.add(oficio)

    s.commit()
    return {"categoria": Categoria.model_validate(categoria), "oficio": Oficio.model_validate(oficio)}


@app.post("/prestador-oficios", tags=["oferta"], status_code=201,
          summary="Registrar que un prestador ofrece un oficio")
def alta_oferta(peticion: AltaOferta, s: Session = Depends(sesion)):
    if s.get(OficioFila, peticion.oficioId) is None:
        raise HTTPException(404, f"Oficio '{peticion.oficioId}' no encontrado")

    existente = s.get(PrestadorOficioFila, (peticion.prestadorId, peticion.oficioId))
    if existente:
        # La oferta es una asociación, no un evento: repetir el alta actualiza.
        existente.tarifaReferencial = peticion.tarifaReferencial
        existente.anosExperiencia = peticion.anosExperiencia
        s.commit()
        return PrestadorOficio.model_validate(existente)

    oferta = PrestadorOficioFila(**peticion.model_dump())
    s.add(oferta)
    s.commit()
    return PrestadorOficio.model_validate(oferta)


@app.post("/contactos", tags=["demanda"], status_code=201,
          summary="Abrir el contacto entre un demandante y un prestador")
def alta_contacto(peticion: AltaContacto, s: Session = Depends(sesion)):
    # Idempotente: volver a contactar a la misma persona reabre el mismo hilo
    # en vez de duplicarlo, que es lo que espera quien pulsa "Contactar".
    existente = s.scalars(select(ContactoFila).where(
        ContactoFila.demandanteId == peticion.demandanteId,
        ContactoFila.prestadorId == peticion.prestadorId,
    )).first()
    if existente:
        return Contacto.model_validate(existente)

    # El origen solo cuenta si es una búsqueda real de este mismo demandante:
    # un id ajeno o inventado no puede inflar la tasa de contacto.
    origen = s.get(BusquedaFila, peticion.busquedaOrigenId) if peticion.busquedaOrigenId else None
    contacto = ContactoFila(
        id=nuevo_id(),
        demandanteId=peticion.demandanteId,
        prestadorId=peticion.prestadorId,
        busquedaOrigenId=origen.id if origen and origen.demandanteId == peticion.demandanteId else None,
        fechaInicio=date.today(),
        estado="ACTIVO",
    )
    s.add(contacto)
    s.commit()
    evento_negocio("contacto_iniciado", "El demandante contactó a un prestador",
                   contactoId=contacto.id, prestadorId=contacto.prestadorId,
                   busquedaOrigenId=contacto.busquedaOrigenId)
    return Contacto.model_validate(contacto)


# --- Catálogo --------------------------------------------------------------
@app.get("/categorias", tags=["catálogo"], response_model=list[Categoria],
         summary="Listar categorías")
def listar_categorias(q: str | None = Query(None), s: Session = Depends(sesion)):
    consulta = select(CategoriaFila).where(*condiciones(
        CategoriaFila.nombre.ilike(f"%{q}%") if q else None,
    )).order_by(CategoriaFila.nombre)
    return listar(s, consulta, Categoria)


@app.get("/categorias/{categoria_id}", tags=["catálogo"], response_model=Categoria,
         summary="Obtener una categoría")
def obtener_categoria(categoria_id: str, s: Session = Depends(sesion)):
    fila = s.get(CategoriaFila, categoria_id)
    if fila is None:
        raise HTTPException(404, f"Categoria '{categoria_id}' no encontrada")
    return Categoria.model_validate(fila)


@app.get("/categorias/{categoria_id}/oficios", tags=["catálogo"], response_model=list[Oficio],
         summary="Oficios agrupados por una categoría")
def oficios_de_categoria(categoria_id: str, s: Session = Depends(sesion)):
    if s.get(CategoriaFila, categoria_id) is None:
        raise HTTPException(404, f"Categoria '{categoria_id}' no encontrada")
    return listar(s, select(OficioFila).where(OficioFila.categoriaId == categoria_id), Oficio)


@app.get("/oficios", tags=["catálogo"], summary="Listar oficios")
def listar_oficios(
    categoriaId: str | None = Query(None),
    q: str | None = Query(None, description="Busca por nombre o descripción"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(OficioFila).where(*condiciones(
        (OficioFila.categoriaId == categoriaId) if categoriaId else None,
        or_(OficioFila.nombre.ilike(f"%{q}%"), OficioFila.descripcion.ilike(f"%{q}%")) if q else None,
    )).order_by(OficioFila.nombre)
    return paginar_consulta(s, consulta, Oficio, page, size)


@app.get("/oficios/{oficio_id}", tags=["catálogo"], response_model=Oficio,
         summary="Obtener un oficio")
def obtener_oficio(oficio_id: str, s: Session = Depends(sesion)):
    fila = s.get(OficioFila, oficio_id)
    if fila is None:
        raise HTTPException(404, f"Oficio '{oficio_id}' no encontrado")
    return Oficio.model_validate(fila)


@app.get("/oficios/{oficio_id}/prestadores", tags=["oferta"],
         summary="Prestadores que ofrecen un oficio (ids y tarifas)")
def prestadores_de_oficio(
    oficio_id: str,
    experienciaMinima: int | None = Query(None, ge=0),
    s: Session = Depends(sesion),
):
    if s.get(OficioFila, oficio_id) is None:
        raise HTTPException(404, f"Oficio '{oficio_id}' no encontrado")
    consulta = select(PrestadorOficioFila).where(*condiciones(
        PrestadorOficioFila.oficioId == oficio_id,
        (PrestadorOficioFila.anosExperiencia >= experienciaMinima)
        if experienciaMinima is not None else None,
    ))
    return listar(s, consulta, PrestadorOficio)


# --- Oferta ----------------------------------------------------------------
@app.get("/prestador-oficios", tags=["oferta"], summary="Asociaciones prestador ↔ oficio")
def listar_prestador_oficios(
    prestadorId: str | None = Query(None),
    oficioId: str | None = Query(None),
    tarifaMaxima: float | None = Query(None, ge=0),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    s: Session = Depends(sesion),
):
    consulta = select(PrestadorOficioFila).where(*condiciones(
        (PrestadorOficioFila.prestadorId == prestadorId) if prestadorId else None,
        (PrestadorOficioFila.oficioId == oficioId) if oficioId else None,
        (PrestadorOficioFila.tarifaReferencial <= tarifaMaxima) if tarifaMaxima is not None else None,
    ))
    return paginar_consulta(s, consulta, PrestadorOficio, page, size)


@app.get("/prestadores/{prestador_id}/oficios", tags=["oferta"],
         summary="Oficios que ofrece un prestador, con el detalle del oficio")
def oficios_de_prestador(prestador_id: str, s: Session = Depends(sesion)):
    # Un solo JOIN dentro del contexto: oferta, oficio y categoría son todos suyos.
    filas = s.execute(
        select(PrestadorOficioFila, OficioFila, CategoriaFila)
        .join(OficioFila, OficioFila.id == PrestadorOficioFila.oficioId)
        .join(CategoriaFila, CategoriaFila.id == OficioFila.categoriaId)
        .where(PrestadorOficioFila.prestadorId == prestador_id)
    ).all()
    items = [{
        "oficio": Oficio.model_validate(oficio),
        "categoria": Categoria.model_validate(categoria),
        "tarifaReferencial": oferta.tarifaReferencial,
        "anosExperiencia": oferta.anosExperiencia,
    } for oferta, oficio, categoria in filas]
    return {"prestadorId": prestador_id, "total": len(items), "items": items}


# --- Demanda ---------------------------------------------------------------
@app.get("/busquedas", tags=["demanda"], summary="Listar búsquedas realizadas")
def listar_busquedas(
    demandanteId: str | None = Query(None),
    categoriaId: str | None = Query(None),
    municipio: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(BusquedaFila).where(*condiciones(
        (BusquedaFila.demandanteId == demandanteId) if demandanteId else None,
        (BusquedaFila.categoriaId == categoriaId) if categoriaId else None,
        func.lower(BusquedaFila.municipio) == municipio.lower() if municipio else None,
    )).order_by(BusquedaFila.fecha.desc())
    return paginar_consulta(s, consulta, Busqueda, page, size)


@app.get("/busquedas/{busqueda_id}", tags=["demanda"], response_model=Busqueda,
         summary="Obtener una búsqueda")
def obtener_busqueda(busqueda_id: str, s: Session = Depends(sesion)):
    fila = s.get(BusquedaFila, busqueda_id)
    if fila is None:
        raise HTTPException(404, f"Busqueda '{busqueda_id}' no encontrada")
    return Busqueda.model_validate(fila)


@app.get("/contactos", tags=["demanda"], summary="Listar contactos")
def listar_contactos(
    demandanteId: str | None = Query(None),
    prestadorId: str | None = Query(None),
    estado: str | None = Query(None, description="ACTIVO o CERRADO"),
    busquedaOrigenId: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(ContactoFila).where(*condiciones(
        (ContactoFila.demandanteId == demandanteId) if demandanteId else None,
        (ContactoFila.prestadorId == prestadorId) if prestadorId else None,
        (func.upper(ContactoFila.estado) == estado.upper()) if estado else None,
        (ContactoFila.busquedaOrigenId == busquedaOrigenId) if busquedaOrigenId else None,
    )).order_by(ContactoFila.fechaInicio.desc())
    return paginar_consulta(s, consulta, Contacto, page, size)


@app.get("/contactos/{contacto_id}", tags=["demanda"], response_model=Contacto,
         summary="Obtener un contacto")
def obtener_contacto(contacto_id: str, s: Session = Depends(sesion)):
    fila = s.get(ContactoFila, contacto_id)
    if fila is None:
        raise HTTPException(404, f"Contacto '{contacto_id}' no encontrado")
    return Contacto.model_validate(fila)


# --- Lecturas agregadas del contexto --------------------------------------
@app.get("/resumen/catalogo", tags=["resumen"],
         summary="Conteo de oficios y prestadores por categoría")
def resumen_catalogo(s: Session = Depends(sesion)):
    # La agregación la hace la base, no el proceso: un GROUP BY por categoría
    # con el conteo de oficios y de ofertas asociadas.
    filas = s.execute(
        select(
            CategoriaFila,
            func.count(func.distinct(OficioFila.id)),
            func.count(PrestadorOficioFila.prestadorId),
        )
        .outerjoin(OficioFila, OficioFila.categoriaId == CategoriaFila.id)
        .outerjoin(PrestadorOficioFila, PrestadorOficioFila.oficioId == OficioFila.id)
        .group_by(CategoriaFila.id)
        .order_by(CategoriaFila.nombre)
    ).all()
    items = [{
        "categoria": Categoria.model_validate(categoria),
        "totalOficios": total_oficios,
        "totalOfertas": total_ofertas,
    } for categoria, total_oficios, total_ofertas in filas]
    return {"total": len(items), "items": items}
