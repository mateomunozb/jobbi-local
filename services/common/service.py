"""Fábrica de aplicaciones FastAPI y utilidades de consulta.

Todos los microservicios de dominio se construyen con `crear_servicio`, de modo
que comparten el mismo contrato operativo: `/health` para las probes de
Kubernetes, `/` con la descripción del contexto y CORS abierto para que el
frontend pueda consumirlos a través del gateway.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Iterable, Sequence, TypeVar

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

T = TypeVar("T")

VERSION = "1.0.0"


def crear_servicio(*, nombre: str, contexto: str, descripcion: str) -> FastAPI:
    """Crea la app del microservicio con los endpoints operativos comunes."""
    app = FastAPI(
        title=f"JOBBI · {contexto}",
        description=descripcion,
        version=VERSION,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["operación"], summary="Liveness / readiness probe")
    def health() -> dict[str, str]:
        return {"status": "UP", "service": nombre, "contexto": contexto, "version": VERSION}

    @app.get("/", tags=["operación"], summary="Descripción del contexto delimitado")
    def raiz() -> dict[str, str]:
        return {
            "servicio": nombre,
            "contexto": contexto,
            "descripcion": descripcion,
            "docs": "/docs",
        }

    return app


class Pagina(BaseModel):
    """Envoltura estándar de toda respuesta de listado."""

    total: int
    page: int
    size: int
    pages: int
    items: list[Any]


def paginar(items: Sequence[T], page: int, size: int) -> Pagina:
    total = len(items)
    pages = (total + size - 1) // size if size else 0
    inicio = (page - 1) * size
    return Pagina(
        total=total,
        page=page,
        size=size,
        pages=pages,
        items=list(items[inicio : inicio + size]),
    )


def filtrar(items: Iterable[T], *predicados: Callable[[T], bool] | None) -> list[T]:
    """Aplica solo los predicados no nulos (los filtros opcionales se omiten)."""
    activos = [p for p in predicados if p is not None]
    return [item for item in items if all(p(item) for p in activos)]


def obtener_o_404(coleccion: Iterable[Any], id_buscado: str, entidad: str) -> Any:
    for item in coleccion:
        if getattr(item, "id", None) == id_buscado:
            return item
    raise HTTPException(status_code=404, detail=f"{entidad} '{id_buscado}' no encontrado")


def puerto_por_defecto(valor: int) -> int:
    return int(os.getenv("PORT", valor))
