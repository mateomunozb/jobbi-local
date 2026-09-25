"""Acceso a datos compartido por todos los contextos.

**Una base de datos por contexto delimitado.** Cada servicio se conecta a su
propia base (`jobbi_identidad`, `jobbi_mercado`, …) con su propia cadena de
conexión. No es una separación por convención: PostgreSQL no permite consultar
entre bases distintas, así que un JOIN accidental entre contextos es imposible
de escribir. Lo único que los relaciona siguen siendo los UUID.

Fuera de Kubernetes, si no hay `DATABASE_URL`, cada servicio cae a un archivo
SQLite propio. Permite levantar todo el backend en local sin instalar nada, y
mantiene la misma separación: un archivo por contexto.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import Select, create_engine, func, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .observabilidad import log, registrar_metricas
from .service import Pagina

E = TypeVar("E", bound=BaseModel)


class Base(DeclarativeBase):
    """Base declarativa. Cada contexto define sus tablas sobre esta."""


def url_de(servicio: str) -> str:
    """Cadena de conexión del contexto, con SQLite como alternativa local."""
    explicita = os.getenv("DATABASE_URL")
    if explicita:
        return explicita

    directorio = Path(os.getenv("SQLITE_DIR", ".datos"))
    directorio.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{directorio / f'{servicio}.db'}"


def crear_motor(servicio: str):
    url = url_de(servicio)
    # check_same_thread solo aplica a SQLite: uvicorn atiende en varios hilos.
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def esperar_a_la_base(motor, intentos: int = 15) -> None:
    """Reintenta con backoff exponencial (1, 2, 4 … 30 s) hasta que PostgreSQL acepte conexiones.

    El Pod del servicio puede arrancar antes que el de la base, o durante una
    caída; sin esto el contenedor entraría en CrashLoopBackOff en lugar de
    esperar. Tras ~5 minutos se rinde y deja que Kubernetes lo reinicie.
    """
    from .resiliencia import Backoff

    backoff = Backoff()
    for intento in range(1, intentos + 1):
        try:
            with motor.connect():
                return
        except OperationalError as e:
            espera = backoff.siguiente()
            log.warning("db_esperando", extra={
                "intento": intento, "de": intentos, "reintentoEnSegundos": round(espera, 1), "error": str(e)})
            time.sleep(espera)
    raise RuntimeError(f"La base de datos no respondió tras {intentos} intentos")


def inicializar(servicio: str) -> sessionmaker[Session]:
    """Prepara el esquema del contexto y devuelve su fábrica de sesiones.

    No se siembra nada: el sistema arranca con las tablas vacías y todo lo que
    existe llega por el uso real de la aplicación. La base es la única fuente
    de verdad; no hay registros de ejemplo escritos en el código.

    `Base.metadata.create_all` basta para este proyecto porque el esquema no
    evoluciona; un sistema real llevaría migraciones versionadas (Alembic).
    """
    motor = crear_motor(servicio)
    esperar_a_la_base(motor)
    Base.metadata.create_all(motor)
    completar_columnas(motor)
    _registrar_metricas_de_pool(motor)
    log.info("db_lista", extra={"contexto": servicio, "base": url_de(servicio).split("@")[-1]})
    return sessionmaker(motor, expire_on_commit=False)


def _registrar_metricas_de_pool(motor) -> None:
    """Saturación de la persistencia: conexiones del pool de este servicio.

    Por defecto SQLAlchemy abre hasta 5 conexiones fijas + 10 de desborde por
    servicio; con 10 servicios son 150 frente a los 100 que admite PostgreSQL.
    Cuando `en_uso` llega a `tamano + desborde_max`, las peticiones esperan
    turno (y a los 30 s fallan): ese es el punto de quiebre del pool.
    """
    pool = motor.pool
    if not hasattr(pool, "checkedout"):
        return
    ayuda = "Conexiones del pool de SQLAlchemy de este servicio"

    def del_pool():
        # Solo lee el estado del pool en memoria: sigue midiendo con la base caída.
        return [
            ("jobbi_db_pool_conexiones", ayuda, {"estado": "en_uso"}, pool.checkedout()),
            ("jobbi_db_pool_conexiones", ayuda, {"estado": "libres"}, pool.checkedin()),
            ("jobbi_db_pool_conexiones", ayuda, {"estado": "desborde"}, max(pool.overflow(), 0)),
            ("jobbi_db_pool_capacidad", "Máximo de conexiones del pool (fijas + desborde)", {},
             pool.size() + getattr(pool, "_max_overflow", 0)),
        ]

    def de_postgres():
        # Consulta a la base: si está caída, solo faltan estas series.
        with motor.connect() as conexion:
            maximo = int(conexion.execute(text("SHOW max_connections")).scalar())
            por_estado = conexion.execute(text(
                "SELECT coalesce(state, 'otro'), count(*) FROM pg_stat_activity "
                "WHERE backend_type = 'client backend' GROUP BY 1")).all()
        return [("jobbi_postgres_conexiones_max", "max_connections de PostgreSQL", {}, maximo)] + [
            ("jobbi_postgres_conexiones", "Conexiones de clientes en PostgreSQL por estado", {"estado": estado}, n)
            for estado, n in por_estado]

    registrar_metricas(del_pool)
    # Un solo servicio mira la base entera, para no contar lo mismo diez veces.
    if os.getenv("METRICAS_POSTGRES", "false").lower() == "true" and motor.dialect.name == "postgresql":
        registrar_metricas(de_postgres)


def completar_columnas(motor) -> None:
    """Añade a las tablas existentes las columnas nuevas del modelo.

    `create_all` crea tablas que no existen, pero no toca las que ya están: si
    el modelo gana una columna, una base con datos se quedaría sin ella. Esta es
    la migración mínima que cubre ese caso, y solo ese: **agrega** columnas (que
    deben ser anulables o tener `server_default`), nunca cambia ni borra nada.
    Un sistema en producción usaría migraciones versionadas (Alembic).
    """
    inspector = inspect(motor)
    existentes = set(inspector.get_table_names())
    with motor.begin() as conexion:
        for tabla in Base.metadata.sorted_tables:
            if tabla.name not in existentes:
                continue
            actuales = {c["name"] for c in inspector.get_columns(tabla.name)}
            for columna in tabla.columns:
                if columna.name in actuales:
                    continue
                tipo = columna.type.compile(dialect=motor.dialect)
                defecto = ""
                if columna.server_default is not None:
                    defecto = f" DEFAULT '{columna.server_default.arg}'"
                conexion.execute(text(
                    f'ALTER TABLE {tabla.name} ADD COLUMN "{columna.name}" {tipo}{defecto}'
                ))
                log.info("db_columna_agregada", extra={"tabla": tabla.name, "columna": columna.name})


# --- Consultas -------------------------------------------------------------

def condiciones(*clausulas) -> list:
    """Descarta los filtros no aplicados, igual que `service.filtrar`.

    Permite escribir los endpoints con la misma forma de antes:
    un filtro opcional se expresa como `(Modelo.campo == valor) if valor else None`.
    """
    return [c for c in clausulas if c is not None]


def paginar_consulta(
    sesion: Session, consulta: Select, esquema: type[E], page: int, size: int,
) -> Pagina:
    """Cuenta en la base y trae solo la página pedida."""
    total = sesion.scalar(select(func.count()).select_from(consulta.subquery())) or 0
    filas = sesion.scalars(consulta.offset((page - 1) * size).limit(size)).all()
    return Pagina(
        total=total,
        page=page,
        size=size,
        pages=(total + size - 1) // size if size else 0,
        items=[esquema.model_validate(fila) for fila in filas],
    )


def listar(sesion: Session, consulta: Select, esquema: type[E]) -> list[E]:
    return [esquema.model_validate(fila) for fila in sesion.scalars(consulta).all()]


def uno(sesion: Session, consulta: Select, esquema: type[E]) -> E | None:
    fila = sesion.scalars(consulta).first()
    return esquema.model_validate(fila) if fila else None



def nuevo_id() -> str:
    """UUID4 para una entidad nueva.

    Los identificadores los genera el contexto dueño de la entidad en el
    momento de crearla. No hay ids reservados ni predecibles: la base es la
    única fuente de verdad sobre qué existe.
    """
    import uuid
    return str(uuid.uuid4())
