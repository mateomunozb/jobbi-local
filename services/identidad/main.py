"""Contexto delimitado: Identidad y Perfiles.

Dueño de Usuario, PerfilDemandante y PerfilPrestador, persistidos en la base
`jobbi_identidad`. Ningún otro servicio alcanza estas tablas: PostgreSQL no
permite consultar entre bases distintas, así que los demás contextos solo
guardan el UUID y preguntan por HTTP.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from common.db import condiciones, listar, paginar_consulta
from common.enums import EstadoCuenta, EstadoVerificacion, PlanPrestador, valores
from common.service import crear_servicio

from . import repositorio
from .auth import router as auth_router
from .models import PerfilDemandante, PerfilPrestador, Usuario
from .tablas import PerfilDemandanteFila, PerfilPrestadorFila, UsuarioFila

app = crear_servicio(
    nombre="identidad",
    contexto="Identidad y Perfiles",
    descripcion=(
        "Cuentas de usuario y los perfiles que activan (demandante y prestador). "
        "Fuente de verdad de la identidad en la plataforma."
    ),
)

# Registro y login: las únicas escrituras del sistema en esta fase.
app.include_router(auth_router)


def sesion() -> Session:
    """Una sesión por petición, cerrada al terminar."""
    with repositorio.abrir() as s:
        yield s


# --- Usuarios --------------------------------------------------------------
@app.get("/usuarios", tags=["usuarios"], summary="Listar usuarios")
def listar_usuarios(
    estado: EstadoCuenta | None = Query(None, description="Filtra por estado de la cuenta"),
    q: str | None = Query(None, description="Busca por nombre o correo"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(UsuarioFila).where(*condiciones(
        (UsuarioFila.estado == estado.value) if estado else None,
        or_(
            UsuarioFila.nombreCompleto.ilike(f"%{q}%"),
            UsuarioFila.correo.ilike(f"%{q}%"),
        ) if q else None,
    )).order_by(UsuarioFila.fechaRegistro)
    return paginar_consulta(s, consulta, Usuario, page, size)


@app.get("/usuarios/{usuario_id}", tags=["usuarios"], response_model=Usuario,
         summary="Obtener un usuario por id")
def obtener_usuario(usuario_id: str, s: Session = Depends(sesion)):
    fila = s.get(UsuarioFila, usuario_id)
    if fila is None:
        raise HTTPException(404, f"Usuario '{usuario_id}' no encontrado")
    return Usuario.model_validate(fila)


@app.get("/usuarios/{usuario_id}/perfiles", tags=["usuarios"],
         summary="Perfiles activados por el usuario")
def perfiles_del_usuario(usuario_id: str, s: Session = Depends(sesion)):
    if s.get(UsuarioFila, usuario_id) is None:
        raise HTTPException(404, f"Usuario '{usuario_id}' no encontrado")
    return {
        "usuarioId": usuario_id,
        "demandante": repositorio.demandante_de(s, usuario_id),
        "prestador": repositorio.prestador_de(s, usuario_id),
    }


# --- Perfil demandante -----------------------------------------------------
@app.get("/demandantes", tags=["demandantes"], summary="Listar perfiles de demandante")
def listar_demandantes(
    municipio: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(PerfilDemandanteFila).where(*condiciones(
        func.lower(PerfilDemandanteFila.municipio) == municipio.lower() if municipio else None,
    )).order_by(PerfilDemandanteFila.fechaActivacion)
    return paginar_consulta(s, consulta, PerfilDemandante, page, size)


@app.get("/demandantes/{demandante_id}", tags=["demandantes"], response_model=PerfilDemandante,
         summary="Obtener un perfil de demandante")
def obtener_demandante(demandante_id: str, s: Session = Depends(sesion)):
    fila = s.get(PerfilDemandanteFila, demandante_id)
    if fila is None:
        raise HTTPException(404, f"PerfilDemandante '{demandante_id}' no encontrado")
    return PerfilDemandante.model_validate(fila)


@app.get("/demandantes/por-usuario/{usuario_id}", tags=["demandantes"],
         response_model=PerfilDemandante, summary="Perfil de demandante de un usuario")
def demandante_por_usuario(usuario_id: str, s: Session = Depends(sesion)):
    perfil = repositorio.demandante_de(s, usuario_id)
    if perfil is None:
        raise HTTPException(404, f"El usuario '{usuario_id}' no tiene perfil de demandante")
    return perfil


# --- Perfil prestador ------------------------------------------------------
@app.get("/prestadores", tags=["prestadores"], summary="Listar perfiles de prestador")
def listar_prestadores(
    verificado: bool | None = Query(None, description="Solo con insignia de verificado"),
    plan: PlanPrestador | None = Query(None),
    estadoVerificacion: EstadoVerificacion | None = Query(None),
    calificacionMinima: float | None = Query(None, ge=0, le=5),
    municipio: str | None = Query(None),
    q: str | None = Query(None, description="Busca en el nombre o la descripción"),
    orden: str = Query("calificacion", pattern="^(calificacion|resenas|tarifa)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    claves = {
        "calificacion": PerfilPrestadorFila.calificacionPromedio.desc(),
        "resenas": PerfilPrestadorFila.totalResenas.desc(),
        "tarifa": PerfilPrestadorFila.tarifaReferencialBase.asc(),
    }
    consulta = select(PerfilPrestadorFila).where(*condiciones(
        (PerfilPrestadorFila.insigniaVerificado == verificado) if verificado is not None else None,
        (PerfilPrestadorFila.planActual == plan.value) if plan else None,
        (PerfilPrestadorFila.estadoVerificacionActual == estadoVerificacion.value)
        if estadoVerificacion else None,
        (PerfilPrestadorFila.calificacionPromedio >= calificacionMinima)
        if calificacionMinima is not None else None,
        func.lower(PerfilPrestadorFila.municipio) == municipio.lower() if municipio else None,
        or_(
            PerfilPrestadorFila.descripcion.ilike(f"%{q}%"),
            PerfilPrestadorFila.nombreCompleto.ilike(f"%{q}%"),
        ) if q else None,
    )).order_by(claves[orden])
    return paginar_consulta(s, consulta, PerfilPrestador, page, size)


@app.get("/prestadores/{prestador_id}", tags=["prestadores"], response_model=PerfilPrestador,
         summary="Obtener un perfil de prestador")
def obtener_prestador(prestador_id: str, s: Session = Depends(sesion)):
    fila = s.get(PerfilPrestadorFila, prestador_id)
    if fila is None:
        raise HTTPException(404, f"PerfilPrestador '{prestador_id}' no encontrado")
    return PerfilPrestador.model_validate(fila)


@app.get("/prestadores/por-usuario/{usuario_id}", tags=["prestadores"],
         response_model=PerfilPrestador, summary="Perfil de prestador de un usuario")
def prestador_por_usuario(usuario_id: str, s: Session = Depends(sesion)):
    perfil = repositorio.prestador_de(s, usuario_id)
    if perfil is None:
        raise HTTPException(404, f"El usuario '{usuario_id}' no tiene perfil de prestador")
    return perfil


# --- Escrituras sobre el perfil del prestador ------------------------------

class CambioPlan(BaseModel):
    plan: PlanPrestador


class ResultadoVerificacion(BaseModel):
    estado: EstadoVerificacion


class ReputacionPrestador(BaseModel):
    """Reputación recalculada por el contexto de Confianza.

    El detalle de las reseñas es de Confianza; aquí solo se guarda el resumen
    que el perfil muestra, para que una tarjeta de búsqueda no tenga que
    preguntarle a otro contexto por cada prestador que pinta.
    """

    calificacionPromedio: float = Field(ge=0, le=5)
    totalResenas: int = Field(ge=0)


def _prestador_o_404(s: Session, prestador_id: str) -> PerfilPrestadorFila:
    fila = s.get(PerfilPrestadorFila, prestador_id)
    if fila is None:
        raise HTTPException(404, f"PerfilPrestador '{prestador_id}' no encontrado")
    return fila


@app.post("/prestadores/{prestador_id}/plan", tags=["prestadores"], response_model=PerfilPrestador,
          summary="Cambiar el plan del prestador")
def cambiar_plan(prestador_id: str, peticion: CambioPlan, s: Session = Depends(sesion)):
    """El plan cambia hacia adelante, nunca hacia atrás.

    Cambiarlo afecta a los acuerdos que se cierren desde ahora. Los servicios
    ya acordados conservan el porcentaje que Contrataciones les escribió al
    cerrar el trato, y este endpoint no los toca.
    """
    fila = _prestador_o_404(s, prestador_id)
    fila.planActual = peticion.plan.value
    s.commit()
    return PerfilPrestador.model_validate(fila)


@app.post("/prestadores/{prestador_id}/reputacion", tags=["prestadores"],
          response_model=PerfilPrestador, summary="Actualizar la reputación publicada del perfil")
def actualizar_reputacion(
    prestador_id: str, peticion: ReputacionPrestador, s: Session = Depends(sesion),
):
    fila = _prestador_o_404(s, prestador_id)
    fila.calificacionPromedio = peticion.calificacionPromedio
    fila.totalResenas = peticion.totalResenas
    s.commit()
    return PerfilPrestador.model_validate(fila)


@app.post("/prestadores/{prestador_id}/verificacion", tags=["prestadores"],
          response_model=PerfilPrestador, summary="Publicar el veredicto de verificación (lo llama Confianza)")
def publicar_verificacion(prestador_id: str, peticion: ResultadoVerificacion, s: Session = Depends(sesion)):
    """RN-01: la insignia refleja el veredicto del aliado, y solo APROBADA la otorga.

    El veredicto es de Confianza; este contexto guarda la copia que muestra el
    perfil y que filtra la búsqueda. El gateway no reenvía esta escritura
    desde afuera: solo la llama Confianza por la red interna.
    """
    fila = _prestador_o_404(s, prestador_id)
    fila.estadoVerificacionActual = peticion.estado.value
    fila.insigniaVerificado = peticion.estado is EstadoVerificacion.APROBADA
    s.commit()
    return PerfilPrestador.model_validate(fila)


@app.post("/demandantes/{demandante_id}/verificacion", tags=["demandantes"],
          response_model=PerfilDemandante, summary="Publicar el veredicto de verificación (lo llama Confianza)")
def publicar_verificacion_demandante(demandante_id: str, peticion: ResultadoVerificacion,
                                     s: Session = Depends(sesion)):
    """RN-01: el demandante también se verifica; solo APROBADA le permite contratar."""
    fila = s.get(PerfilDemandanteFila, demandante_id)
    if fila is None:
        raise HTTPException(404, f"PerfilDemandante '{demandante_id}' no encontrado")
    fila.estadoVerificacionActual = peticion.estado.value
    s.commit()
    return PerfilDemandante.model_validate(fila)


# --- Catálogos de enumeraciones -------------------------------------------
@app.get("/enums", tags=["catálogos"], summary="Enumeraciones que expone este contexto")
def enumeraciones():
    return {
        "estadoCuenta": valores(EstadoCuenta),
        "estadoVerificacion": valores(EstadoVerificacion),
        "planPrestador": valores(PlanPrestador),
    }
