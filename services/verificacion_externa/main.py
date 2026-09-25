"""Simulador del aliado externo de verificación (tipo Truora). NO es el servicio real.

Existe para dos cosas:

1. Darle a Confianza una dependencia externa de verdad —otro proceso, al otro
   lado de la red, con **su propio formato**— contra la que tenga sentido el
   Adapter (Anti-Corruption Layer) y el Circuit Breaker.
2. Inyectar el **Fallo 1** del plan de pruebas: retardo prolongado y respuestas
   504, controlados en caliente por `/_caos`, sin reiniciar nada.

Veredicto pseudoaleatorio: se rechaza (antecedentes con hallazgos) un
PORCENTAJE_RECHAZO de los documentos, 20 % por defecto. Parece aleatorio, pero
lo decide un hash del número de documento: el mismo documento da siempre el
mismo resultado, así una prueba se puede repetir.

Formato propietario (lo que Confianza traduce, y nunca deja pasar a su dominio):

    POST /v1/checks  {"type": "background_check", "country": "CO",
                      "national_id": "<documento>", "user_reference": "<id>"}
    → {"check_id": "CHK…", "status": "completed", "score": 0.94,
       "summary": {"identity": {"result": "valid"}, "background": {"result": "clear"}}}

Control del fallo:

    POST   /_caos {"latenciaMs": 8000, "codigo": 504}   # retardo y/o código de error
    GET    /_caos                                        # configuración vigente
    DELETE /_caos                                        # vuelve a responder normal
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timezone

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common.observabilidad import log
from common.service import crear_servicio

app = crear_servicio(
    nombre="verificacion-externa",
    contexto="Aliado de verificación (simulado)",
    descripcion="Doble de prueba del aliado de verificación de identidad y antecedentes.",
)

# Referencias cuyo chequeo de antecedentes sale con hallazgos (para probar el
# rechazo de RN-01 de punta a punta). Se registran por /_caos.
_caos: dict = {"latenciaMs": 0, "codigo": None, "antecedentesConHallazgos": []}

PORCENTAJE_RECHAZO = int(os.getenv("PORCENTAJE_RECHAZO", "20"))


def con_hallazgos(documento: str) -> bool:
    """¿Los antecedentes de este documento salen con hallazgos? (determinista)"""
    if not documento:
        return False
    return int(hashlib.sha256(documento.encode()).hexdigest()[:8], 16) % 100 < PORCENTAJE_RECHAZO


class ConfiguracionCaos(BaseModel):
    latenciaMs: int = Field(default=0, ge=0, le=120000)
    codigo: int | None = Field(default=None, ge=400, le=599)
    antecedentesConHallazgos: list[str] = []


@app.post("/v1/checks", status_code=201, tags=["aliado"], summary="Crear un chequeo (formato del aliado)")
async def crear_chequeo(cuerpo: dict):
    if _caos["latenciaMs"]:
        # Asíncrono: el simulador sigue atendiendo aunque cada respuesta tarde.
        await asyncio.sleep(_caos["latenciaMs"] / 1000)
    if _caos["codigo"]:
        return JSONResponse(status_code=_caos["codigo"],
                            content={"code": _caos["codigo"], "message": "upstream timeout"})

    referencia = str(cuerpo.get("user_reference", ""))
    hallazgos = (referencia in _caos["antecedentesConHallazgos"]
                 or con_hallazgos(str(cuerpo.get("national_id", ""))))
    return {
        "check_id": f"CHK{uuid.uuid4().hex[:24]}",
        "type": cuerpo.get("type", "background_check"),
        "country": cuerpo.get("country", "CO"),
        "status": "completed",
        "score": 0.21 if hallazgos else 0.94,
        "creation_date": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "identity": {"result": "valid"},
            "background": {"result": "found" if hallazgos else "clear"},
        },
    }


@app.get("/_caos", tags=["caos"], summary="Fallo inyectado vigente")
def ver_caos():
    return _caos


@app.post("/_caos", tags=["caos"], summary="Inyectar retardo y/o código de error")
def inyectar_caos(config: ConfiguracionCaos):
    _caos.update(config.model_dump())
    log.warning("caos_inyectado", extra={"fallo": _caos})
    return _caos


@app.delete("/_caos", tags=["caos"], summary="Volver a responder con normalidad")
def retirar_caos():
    _caos.update(latenciaMs=0, codigo=None, antecedentesConHallazgos=[])
    log.info("caos_retirado")
    return _caos
