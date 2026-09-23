"""Acceso a la base del contexto Identidad.

Las tablas arrancan vacías: el primer usuario existe cuando alguien se registra,
no antes. La base es la única fuente de verdad.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from common.db import inicializar

from .models import PerfilDemandante, PerfilPrestador, Usuario
from .tablas import PerfilDemandanteFila, PerfilPrestadorFila, UsuarioFila


Sesion: sessionmaker[Session] = inicializar("identidad")


def abrir() -> Session:
    return Sesion()


# --- Lecturas --------------------------------------------------------------

def usuario_por_correo(sesion: Session, correo: str) -> Usuario | None:
    # El correo se normaliza a minúsculas al guardarlo, así que basta comparar
    # contra la versión normalizada de lo que llega.
    fila = sesion.scalars(
        select(UsuarioFila).where(UsuarioFila.correo == correo.strip().lower())
    ).first()
    return Usuario.model_validate(fila) if fila else None


def demandante_de(sesion: Session, usuario_id: str) -> PerfilDemandante | None:
    fila = sesion.scalars(
        select(PerfilDemandanteFila).where(PerfilDemandanteFila.usuarioId == usuario_id)
    ).first()
    return PerfilDemandante.model_validate(fila) if fila else None


def prestador_de(sesion: Session, usuario_id: str) -> PerfilPrestador | None:
    fila = sesion.scalars(
        select(PerfilPrestadorFila).where(PerfilPrestadorFila.usuarioId == usuario_id)
    ).first()
    return PerfilPrestador.model_validate(fila) if fila else None


# --- Escrituras (registro) -------------------------------------------------

def crear_usuario(sesion: Session, usuario: Usuario) -> None:
    sesion.add(UsuarioFila(**usuario.model_dump()))
    # Se escribe de inmediato, dentro de la misma transacción. Los perfiles lo
    # referencian por clave foránea y PostgreSQL la valida en el INSERT; como
    # las tablas no están unidas por un relationship() del ORM, SQLAlchemy no
    # deduce el orden por sí solo. El flush aquí evita que quien cree un perfil
    # tenga que acordarse.
    sesion.flush()


def crear_demandante(sesion: Session, perfil: PerfilDemandante) -> None:
    sesion.add(PerfilDemandanteFila(
        id=perfil.id, usuarioId=perfil.usuarioId, nombreCompleto=perfil.nombreCompleto,
        fechaActivacion=perfil.fechaActivacion, **perfil.ubicacionPrincipal.model_dump(),
    ))


def crear_prestador(sesion: Session, perfil: PerfilPrestador) -> None:
    volcado = perfil.model_dump(exclude={"ubicacionPrincipal"})
    sesion.add(PerfilPrestadorFila(**volcado, **perfil.ubicacionPrincipal.model_dump()))
