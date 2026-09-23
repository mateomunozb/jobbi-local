"""Registro de servicios de dominio.

Cada entrada se puede sobrescribir por variable de entorno, de modo que el
mismo binario sirve para correr en Kubernetes (DNS interno del clúster) y en
local (localhost con puertos distintos), sin recompilar la imagen.
"""

from __future__ import annotations

import os

NAMESPACE = os.getenv("K8S_NAMESPACE", "aws-local")

# nombre lógico -> (host por defecto en el clúster, puerto, descripción)
_DEFINICION = {
    "identidad": (f"identidad.{NAMESPACE}.svc.cluster.local", 8001, "Identidad y Perfiles"),
    "mercado": (f"mercado.{NAMESPACE}.svc.cluster.local", 8002, "Mercado de Oficios"),
    "contrataciones": (f"contrataciones.{NAMESPACE}.svc.cluster.local", 8003, "Contrataciones"),
    "comunicacion": (f"comunicacion.{NAMESPACE}.svc.cluster.local", 8004, "Comunicación"),
    "confianza": (f"confianza.{NAMESPACE}.svc.cluster.local", 8005, "Confianza y Verificación"),
    "monetizacion": (
        f"servicio-monetizacion.{NAMESPACE}.svc.cluster.local", 8000, "Monetización",
    ),
    "soporte": (f"soporte.{NAMESPACE}.svc.cluster.local", 8006, "Soporte y Disputas"),
    "adquisicion": (f"adquisicion.{NAMESPACE}.svc.cluster.local", 8007, "Adquisición y Distribución"),
    "proteccion": (f"proteccion.{NAMESPACE}.svc.cluster.local", 8008, "Protección / Seguros"),
}


def _url(nombre: str, host: str, puerto: int) -> str:
    # Ej.: SERVICIO_IDENTIDAD_URL=http://localhost:8001
    return os.getenv(f"SERVICIO_{nombre.upper()}_URL", f"http://{host}:{puerto}")


SERVICIOS: dict[str, dict[str, str]] = {
    nombre: {"url": _url(nombre, host, puerto), "contexto": contexto}
    for nombre, (host, puerto, contexto) in _DEFINICION.items()
}
