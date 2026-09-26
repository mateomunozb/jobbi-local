"""Cobertura geográfica de la Fase 1 (RN-09).

Registrar un perfil (demandante o prestador) y acordar una contratación solo se
permite dentro del Valle de Aburrá (restricción R5 del caso de negocio). Vive en
`common` porque la aplican dos contextos: Identidad al registrar y el gateway al
cerrar un acuerdo.
"""

from __future__ import annotations

import unicodedata

MUNICIPIOS_VALLE_DE_ABURRA = (
    "Medellín", "Bello", "Itagüí", "Envigado", "Sabaneta",
    "La Estrella", "Caldas", "Copacabana", "Girardota", "Barbosa",
)


def _normalizar(nombre: str) -> str:
    # "Itagui", "ITAGÜÍ" e "Itagüí" son el mismo municipio.
    sin_tildes = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.lower().split())


_COBERTURA = {_normalizar(m) for m in MUNICIPIOS_VALLE_DE_ABURRA}


def en_cobertura(municipio: str | None) -> bool:
    """¿El municipio pertenece al Valle de Aburrá?"""
    return bool(municipio) and _normalizar(municipio) in _COBERTURA
