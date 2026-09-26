"""Adapter (Anti-Corruption Layer) hacia el aliado de verificación, tras un Circuit Breaker.

El aliado habla su propio idioma (`check_id`, `summary.background.result =
"clear" | "found"`, …). Este módulo es el único que lo conoce: hacia adentro
solo salen `EstadoVerificacion` del dominio, uno por tipo de verificación
(IDENTIDAD y ANTECEDENTES). Cambiar de aliado es reescribir `traducir`, no el
resto de Confianza.

Toda llamada pasa por un Circuit Breaker: si el aliado se pone lento o
responde 5xx, tras `CB_UMBRAL_FALLOS` fallos seguidos el circuito se abre y,
durante `CB_ESPERA_SEGUNDOS`, Confianza ni lo intenta: responde de inmediato con
el último estado conocido. Así un aliado de 8 s no se lleva por delante el
cierre de acuerdos del marketplace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from common.enums import EstadoVerificacion
from common.observabilidad import cabeceras_de_traza
from common.resiliencia import CircuitBreaker

VERIFICACION_URL = os.getenv(
    "VERIFICACION_URL", "http://verificacion-externa.aws-local.svc.cluster.local:8010"
)
TIMEOUT_SEGUNDOS = float(os.getenv("VERIFICACION_TIMEOUT_SEGUNDOS", "2.0"))

IDENTIDAD, ANTECEDENTES = "IDENTIDAD", "ANTECEDENTES"

circuito = CircuitBreaker(
    "verificacion-externa",
    umbral_fallos=int(os.getenv("CB_UMBRAL_FALLOS", "3")),
    espera_segundos=float(os.getenv("CB_ESPERA_SEGUNDOS", "30")),
)


@dataclass(frozen=True)
class ResultadoVerificacion:
    """El veredicto del aliado, ya en términos del dominio."""

    estados: dict[str, EstadoVerificacion]
    referenciaExterna: str
    detalle: str


_IDENTIDAD = {"valid": EstadoVerificacion.APROBADA, "invalid": EstadoVerificacion.RECHAZADA}
_ANTECEDENTES = {"clear": EstadoVerificacion.APROBADA, "found": EstadoVerificacion.RECHAZADA}


def traducir(respuesta: dict[str, Any]) -> ResultadoVerificacion:
    """Formato del aliado → dominio. Lo que el aliado no dice con certeza queda PENDIENTE."""
    resumen = respuesta.get("summary") or {}
    terminado = respuesta.get("status") == "completed"

    def estado(tabla: dict, clave: str) -> EstadoVerificacion:
        if not terminado:
            return EstadoVerificacion.PENDIENTE
        return tabla.get((resumen.get(clave) or {}).get("result"), EstadoVerificacion.PENDIENTE)

    estados = {IDENTIDAD: estado(_IDENTIDAD, "identity"), ANTECEDENTES: estado(_ANTECEDENTES, "background")}
    return ResultadoVerificacion(
        estados=estados,
        referenciaExterna=str(respuesta.get("check_id", "")),
        detalle=f"score={respuesta.get('score')} identidad={estados[IDENTIDAD].value} "
                f"antecedentes={estados[ANTECEDENTES].value}",
    )


def insignia_vigente(estados: dict[str, EstadoVerificacion]) -> bool:
    """RN-01: la insignia de verificado exige identidad Y antecedentes aprobados."""
    return (estados.get(IDENTIDAD) == EstadoVerificacion.APROBADA
            and estados.get(ANTECEDENTES) == EstadoVerificacion.APROBADA)


def verificar(prestador_id: str, documento: str) -> ResultadoVerificacion:
    """Consulta al aliado a través del circuito.

    Lanza `CircuitoAbierto` si no se intentó, o el error HTTP si el aliado falló.
    """
    def consultar() -> ResultadoVerificacion:
        respuesta = httpx.post(
            f"{VERIFICACION_URL}/v1/checks",
            json={"type": "background_check", "country": "CO", "national_id": documento,
                  "user_reference": prestador_id},
            timeout=httpx.Timeout(TIMEOUT_SEGUNDOS, connect=1.0),
            headers=cabeceras_de_traza(),
        )
        respuesta.raise_for_status()
        return traducir(respuesta.json())

    return circuito.llamar(consultar)
