"""El proxy de escritura del gateway solo deja pasar lo que no tiene reglas de dominio.

Cada caso rechazado es un atajo que, reenviado tal cual, se saltaría una regla:
aceptar un acuerdo con comisión 0 %, cargar una comisión sin check-out, fijar
una reputación, cambiar de plan en un solo contexto o inflar el match rate.

    cd services && ../.venv/bin/python -m pytest -v tests/test_gateway_proxy.py
"""

from __future__ import annotations

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import main as gateway


@pytest.fixture
def api(monkeypatch):
    reenviados = []

    async def _enviar(servicio, ruta, cuerpo):
        reenviados.append(f"{servicio}{ruta}")
        return JSONResponse(status_code=201, content={"ok": True})

    monkeypatch.setattr(gateway, "_enviar", _enviar)
    with TestClient(gateway.app) as cliente:
        cliente.reenviados = reenviados
        yield cliente


@pytest.mark.parametrize("ruta", [
    "contrataciones/acuerdos/a-1/aceptar",          # comisión 0 %, sin RN-01 ni RN-04
    "contrataciones/acuerdos",
    "contrataciones/contrataciones/c-1/check-out",  # cierre sin la ruta del ciclo de vida
    "contrataciones/outbox/publicar",
    "monetizacion/comisiones",                      # cobro sin check-out ni outbox
    "monetizacion/cobrar",
    "monetizacion/suscripciones",                   # plan cambiado en un solo contexto
    "identidad/prestadores/p-1/plan",
    "identidad/prestadores/p-1/reputacion",         # reputación a mano
    "mercado/busquedas",                            # inflar el match rate
    "mercado/contactos",
    "confianza/resenas",
    "confianza/prestadores/p-1/verificacion",
    "soporte/incidentes",
    "comunicacion/conversaciones/x/cerrar",
])
def test_las_escrituras_con_reglas_no_pasan_por_el_proxy(api, ruta):
    respuesta = api.post(f"/api/{ruta}", json={"porcentajeComision": 0})
    assert respuesta.status_code == 403
    assert "/api/bff/" in respuesta.json()["detail"]
    assert api.reenviados == []


@pytest.mark.parametrize("ruta", [
    "comunicacion/mensajes",
    "mercado/catalogo/oficio",
    "mercado/prestador-oficios",
])
def test_las_escrituras_sin_reglas_cruzadas_pasan(api, ruta):
    assert api.post(f"/api/{ruta}", json={}).status_code == 201
    assert api.reenviados == [f"{ruta.split('/', 1)[0]}/{ruta.split('/', 1)[1]}"]


def test_el_proxy_remite_a_la_ruta_bff_que_corresponde(api):
    detalle = api.post("/api/mercado/contactos", json={}).json()["detail"]
    assert "POST /api/bff/contactar" in detalle


def test_monetizacion_no_expone_cobros_sincronos():
    from monetizacion import main as monetizacion
    rutas = {(r.path, m) for r in monetizacion.app.routes for m in getattr(r, "methods", set())}
    assert ("/comisiones", "POST") not in rutas
    assert ("/cobrar", "POST") not in rutas
