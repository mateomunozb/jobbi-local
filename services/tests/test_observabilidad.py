"""Observabilidad: /metrics (RED y negocio), logs JSON y traceId de punta a punta.

    cd services && ../.venv/bin/python -m pytest -v tests/test_observabilidad.py
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi.testclient import TestClient

from common import eventos
from common.observabilidad import FormatoJson, usar_trace_id
from contrataciones import main as contrataciones
from contrataciones.tablas import EventoOutboxFila
from mercado import main as mercado
from monetizacion import main as monetizacion
from monetizacion.especificaciones import UMBRAL_BLOQUEO
from monetizacion.sqs_worker import procesar_mensaje
from monetizacion.tablas import EventoOutboxMonetizacionFila

api_contrataciones = TestClient(contrataciones.app)
api_monetizacion = TestClient(monetizacion.app)


def _contratacion_completada(valor: float, traza: str) -> dict:
    cabeceras = {"X-Trace-Id": traza}
    acuerdo = api_contrataciones.post("/acuerdos", headers=cabeceras, json={
        "contactoId": str(uuid.uuid4()), "demandanteId": str(uuid.uuid4()),
        "prestadorId": str(uuid.uuid4()), "oficioId": str(uuid.uuid4()),
        "valorPropuesto": valor, "medioPago": "EFECTIVO", "propuestoPor": "DEMANDANTE",
    }).json()
    contratacion = api_contrataciones.post(f"/acuerdos/{acuerdo['id']}/aceptar", headers=cabeceras, json={
        "rol": "PRESTADOR", "porcentajeComision": 0.18}).json()["contratacion"]
    api_contrataciones.post(f"/contrataciones/{contratacion['id']}/check-in", headers=cabeceras)
    return api_contrataciones.post(f"/contrataciones/{contratacion['id']}/check-out", headers=cabeceras).json()


def _payload_outbox(contratacion_id: str) -> dict:
    with contrataciones.Sesion() as s:
        fila = s.query(EventoOutboxFila).filter_by(agregadoId=contratacion_id).one()
    return json.loads(fila.payload)


def _como_mensaje_sqs(payload: dict) -> str:
    return json.dumps({"Type": "Notification", "MessageId": str(uuid.uuid4()),
                       "TopicArn": "arn:tema", "Message": json.dumps(payload)})


# --- /metrics ---------------------------------------------------------------

def test_metrics_expone_el_histograma_red_con_la_plantilla_de_la_ruta():
    api_contrataciones.get(f"/contrataciones/{uuid.uuid4()}")  # 404, pero se mide
    texto = api_contrataciones.get("/metrics").text
    assert "jobbi_http_request_duration_seconds_bucket" in texto
    assert 'route="/contrataciones/{contratacion_id}"' in texto
    assert 'status="404"' in texto
    # Las probes de Kubernetes no cuentan como tráfico del negocio.
    assert 'route="/health"' not in texto


def test_metrics_expone_las_metricas_de_negocio():
    _contratacion_completada(100000, uuid.uuid4().hex)
    texto = api_monetizacion.get("/metrics").text
    for nombre in ("jobbi_comisiones_facturadas_efectivo_pesos", "jobbi_comisiones_cobradas_pesos",
                   "jobbi_billeteras_bloqueadas", "jobbi_saldo_pendiente_pesos", "jobbi_outbox_pendientes",
                   "jobbi_busquedas_registradas", "jobbi_contactos_iniciados"):
        assert nombre in texto, nombre


# --- traceId ----------------------------------------------------------------

def test_la_respuesta_devuelve_el_trace_id_recibido():
    respuesta = api_contrataciones.get("/health", headers={"X-Trace-Id": "traza-123"})
    assert respuesta.headers["X-Trace-Id"] == "traza-123"


def test_el_trace_id_del_check_out_viaja_hasta_el_aviso_de_bloqueo():
    """Check-out (Contrataciones) → evento → cobro (Monetización) → BILLETERA_BLOQUEADA."""
    traza = uuid.uuid4().hex
    contratacion = _contratacion_completada(UMBRAL_BLOQUEO / 0.18 + 1000, traza)
    payload = _payload_outbox(contratacion["id"])
    assert payload["traceId"] == traza

    usar_trace_id("otra-traza")  # el worker corre en otro contexto
    assert procesar_mensaje(monetizacion.Sesion, _como_mensaje_sqs(payload), "sqs-t") == "COMISION_COBRADA"

    with monetizacion.Sesion() as s:
        aviso = s.query(EventoOutboxMonetizacionFila).filter(
            EventoOutboxMonetizacionFila.tipo == eventos.BILLETERA_BLOQUEADA,
            EventoOutboxMonetizacionFila.payload.contains(contratacion["id"]),
        ).one()
    assert json.loads(aviso.payload)["traceId"] == traza


# --- Logs JSON ----------------------------------------------------------------

def test_el_log_es_json_con_los_campos_acordados():
    usar_trace_id("traza-log")
    registro = logging.LogRecord("jobbi", logging.INFO, __file__, 1, "Comisión cargada", None, None)
    registro.business_event = "comision_aplicada"
    registro.contratacionId = "c-1"
    linea = json.loads(FormatoJson().format(registro))
    assert linea["traceId"] == "traza-log"
    assert linea["business_event"] == "comision_aplicada"
    assert linea["contratacionId"] == "c-1"
    assert {"timestamp", "level", "service", "message"} <= set(linea)


# --- Match rate: búsqueda registrada y contacto que la cita ------------------

def _metrica(texto: str, nombre: str) -> float:
    return next(float(l.split()[-1]) for l in texto.splitlines() if l.startswith(nombre + " "))


def test_el_contacto_que_nace_de_una_busqueda_cuenta_para_el_match_rate():
    api_mercado = TestClient(mercado.app)
    antes = api_mercado.get("/metrics").text
    demandante = str(uuid.uuid4())

    busqueda = api_mercado.post("/busquedas", json={"demandanteId": demandante}).json()
    api_mercado.post("/contactos", json={"demandanteId": demandante, "prestadorId": str(uuid.uuid4()),
                                         "busquedaOrigenId": busqueda["id"]})
    # Contacto sin búsqueda (desde el inicio): no suma al numerador.
    api_mercado.post("/contactos", json={"demandanteId": demandante, "prestadorId": str(uuid.uuid4())})

    despues = api_mercado.get("/metrics").text
    assert _metrica(despues, "jobbi_busquedas_registradas") == _metrica(antes, "jobbi_busquedas_registradas") + 1
    assert _metrica(despues, "jobbi_contactos_iniciados") == _metrica(antes, "jobbi_contactos_iniciados") + 1


def test_una_busqueda_ajena_no_se_acepta_como_origen():
    api_mercado = TestClient(mercado.app)
    busqueda = api_mercado.post("/busquedas", json={"demandanteId": "otro-demandante"}).json()
    contacto = api_mercado.post("/contactos", json={
        "demandanteId": str(uuid.uuid4()), "prestadorId": str(uuid.uuid4()),
        "busquedaOrigenId": busqueda["id"]}).json()
    assert contacto["busquedaOrigenId"] is None
