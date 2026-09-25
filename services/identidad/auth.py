"""Registro y autenticación simplificada.

**Verificación del prestador:** nace PENDIENTE y sin insignia. El veredicto lo
decide Confianza con el aliado de verificación (a través del gateway, al
registrarse) y se lo comunica a este contexto por
`POST /prestadores/{id}/verificacion` (RN-01).

**Decisión explícita de alcance:** el login reconoce al usuario únicamente por su
correo, sin contraseña ni token. Es lo que se pidió para no bloquear el avance
del resto del sistema; no es un esquema de autenticación real y no debe salir
de este entorno de demostración.

Cuando se implemente de verdad, lo que cambia es el cuerpo de estas funciones:
la forma de las respuestas (`SesionResponse`) ya contempla el estado de
verificación, así que el frontend no tendría que rediseñarse.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from common.db import nuevo_id
from common.enums import EstadoCuenta, EstadoVerificacion, PlanPrestador

from . import repositorio
from .models import PerfilDemandante, PerfilPrestador, Ubicacion, Usuario

router = APIRouter(tags=["auth"])

# Coordenadas aproximadas del centro de cada municipio con cobertura. No son un
# registro de dominio ni se guardan como tal: solo ubican el perfil en el mapa
# mientras la persona no defina una dirección exacta.
_COORDENADAS = {
    "Medellín": (6.2442, -75.5812),
    "Envigado": (6.1722, -75.5906),
    "Bello": (6.3373, -75.5540),
    "Itagüí": (6.1719, -75.6116),
    "Sabaneta": (6.1515, -75.6166),
}


class RegistroRequest(BaseModel):
    nombreCompleto: str = Field(min_length=3, max_length=120)
    correo: EmailStr
    telefono: str = Field(min_length=7, max_length=30)
    tipoDocumento: str = Field(default="CC", max_length=10)
    numeroDocumento: str = Field(min_length=4, max_length=30)
    rol: str = Field(pattern="^(Demandante|Prestador)$")

    municipio: str = Field(default="Medellín", max_length=80)
    comuna: str | None = None
    barrio: str | None = None

    # Solo para prestador.
    descripcion: str | None = None
    tarifaReferencialBase: float | None = Field(default=None, ge=0)


class LoginRequest(BaseModel):
    correo: EmailStr


class SesionResponse(BaseModel):
    """Lo que el frontend necesita para decidir qué pantalla mostrar."""

    usuario: Usuario
    rol: str
    perfilDemandante: PerfilDemandante | None = None
    perfilPrestador: PerfilPrestador | None = None
    # Para un prestador, si ya tiene la insignia (RN-01); un demandante no se verifica.
    verificado: bool = True


def sesion_db() -> Session:
    with repositorio.abrir() as s:
        yield s


def _sesion_de(s: Session, usuario: Usuario) -> SesionResponse:
    demandante = repositorio.demandante_de(s, usuario.id)
    prestador = repositorio.prestador_de(s, usuario.id)
    return SesionResponse(
        usuario=usuario,
        rol="Prestador" if prestador else "Demandante",
        perfilDemandante=demandante,
        perfilPrestador=prestador,
        verificado=prestador.insigniaVerificado if prestador else True,
    )


@router.post("/auth/registro", response_model=SesionResponse, status_code=201,
             summary="Registrar un usuario y activar su perfil")
def registrar(peticion: RegistroRequest, s: Session = Depends(sesion_db)) -> SesionResponse:
    if repositorio.usuario_por_correo(s, peticion.correo):
        raise HTTPException(409, f"Ya existe una cuenta con el correo '{peticion.correo}'")

    hoy = date.today()
    usuario = Usuario(
        id=nuevo_id(),
        nombreCompleto=peticion.nombreCompleto.strip(),
        correo=peticion.correo.strip().lower(),
        telefono=peticion.telefono.strip(),
        tipoDocumento=peticion.tipoDocumento,
        numeroDocumento=peticion.numeroDocumento.strip(),
        fechaRegistro=hoy,
        estado=EstadoCuenta.ACTIVO,
    )
    repositorio.crear_usuario(s, usuario)

    latitud, longitud = _COORDENADAS.get(peticion.municipio, _COORDENADAS["Medellín"])
    ubicacion = Ubicacion(
        municipio=peticion.municipio,
        comuna=peticion.comuna or "",
        barrio=peticion.barrio or "",
        latitud=latitud,
        longitud=longitud,
    )

    if peticion.rol == "Prestador":
        repositorio.crear_prestador(s, PerfilPrestador(
            id=nuevo_id(),
            usuarioId=usuario.id,
            nombreCompleto=usuario.nombreCompleto,
            telefono=usuario.telefono,
            descripcion=peticion.descripcion or "",
            portafolioUrl="",
            tarifaReferencialBase=peticion.tarifaReferencialBase or 0,
            # Sin insignia hasta que el aliado de verificación lo apruebe (RN-01).
            insigniaVerificado=False,
            estadoVerificacionActual=EstadoVerificacion.PENDIENTE,
            planActual=PlanPrestador.FREE,
            # Nace sin reputación: la construye con reseñas reales.
            calificacionPromedio=0,
            totalResenas=0,
            fechaActivacion=hoy,
            ubicacionPrincipal=ubicacion,
        ))
    else:
        repositorio.crear_demandante(s, PerfilDemandante(
            id=nuevo_id(),
            usuarioId=usuario.id,
            nombreCompleto=usuario.nombreCompleto,
            ubicacionPrincipal=ubicacion,
            fechaActivacion=hoy,
        ))

    # Un solo commit: si algo falla, no queda un usuario sin su perfil.
    s.commit()
    return _sesion_de(s, usuario)


@router.post("/auth/login", response_model=SesionResponse,
             summary="Iniciar sesión solo con el correo")
def login(peticion: LoginRequest, s: Session = Depends(sesion_db)) -> SesionResponse:
    usuario = repositorio.usuario_por_correo(s, peticion.correo)
    if usuario is None:
        raise HTTPException(404, f"No existe una cuenta con el correo '{peticion.correo}'")
    # Se valida únicamente que el correo exista, según el alcance acordado. El
    # EstadoCuenta viaja en la respuesta (`usuario.estado`) por si más adelante
    # se decide bloquear cuentas suspendidas: el dato ya está en el cliente.
    return _sesion_de(s, usuario)
