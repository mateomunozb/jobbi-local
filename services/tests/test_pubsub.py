"""Pruebas del patrón Pub/Sub: outbox del productor y consumidor idempotente.

    cd services && ../.venv/bin/python -m pytest -v
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from contrataciones import main as contrataciones
from contrataciones.outbox import RelayOutbox
from contrataciones.tablas import EventoOutboxFila
from monetizacion import main as monetizacion
from monetizacion.sqs_worker import procesar_mensaje

api_contrataciones = TestClient(contrataciones.app)
api_monetizacion = TestClient(monetizacion.app)


def _contratacion_completada(valor: float = 100000, porcentaje: float = 0.18) -> dict:
    """Recorre el flujo real: propuesta, aceptación, check-in y check-out."""
    prestador = str(uuid.uuid4())
    acuerdo = api_contrataciones.post("/acuerdos", json={
        "contactoId": str(uuid.uuid4()), "demandanteId": str(uuid.uuid4()),
        "prestadorId": prestador, "oficioId": str(uuid.uuid4()),
        "valorPropuesto": valor, "medioPago": "EFECTIVO", "propuestoPor": "DEMANDANTE",
    }).json()
    cerrado = api_contrataciones.post(f"/acuerdos/{acuerdo['id']}/aceptar", json={
        "rol": "PRESTADOR", "porcentajeComision": porcentaje, "planPrestador": "FREE",
    }).json()
    contratacion_id = cerrado["contratacion"]["id"]
    assert api_contrataciones.post(f"/contrataciones/{contratacion_id}/check-in").status_code == 200
    respuesta = api_contrataciones.post(f"/contrataciones/{contratacion_id}/check-out")
    assert respuesta.status_code == 200
    return respuesta.json()


def _eventos_de(contratacion_id: str) -> list[EventoOutboxFila]:
    with contrataciones.Sesion() as s:
        return s.query(EventoOutboxFila).filter_by(agregadoId=contratacion_id).all()


class PublicadorFalso:
    """Sustituye a SNS: guarda lo publicado y puede simular una caída."""

    def __init__(self, caido: bool = False) -> None:
        self.caido = caido
        self.mensajes: list[dict] = []

    def __call__(self, lote):
        if self.caido:
            raise ConnectionError("LocalStack no responde")
        publicados = {}
        for evento_id, _tipo, payload in lote:
            sns_id = str(uuid.uuid4())
            self.mensajes.append({"eventoId": evento_id, "snsId": sns_id, "payload": payload})
            publicados[evento_id] = sns_id
        return publicados

    def como_mensaje_sqs(self, indice: int = -1) -> str:
        """Lo que recibe el worker: SNS envuelve el evento en su propio sobre."""
        m = self.mensajes[indice]
        return json.dumps({
            "Type": "Notification", "MessageId": m["snsId"],
            "TopicArn": "arn:aws:sns:us-east-1:000000000000:contratacion-completada-topic",
            "Message": m["payload"],
        })


def _publicar_todo(publicador: PublicadorFalso) -> None:
    relay = RelayOutbox(contrataciones.Sesion, publicar=publicador)
    while relay.publicar_pendientes():
        pass


def _saldo(prestador_id: str) -> float:
    resumen = api_monetizacion.get(f"/resumen/prestador/{prestador_id}").json()
    return resumen["billetera"]["saldoPendiente"] if resumen["billetera"] else 0.0


# --- Productor: Transactional Outbox ---------------------------------------

def test_checkout_guarda_el_evento_junto_con_el_cierre():
    contratacion = _contratacion_completada(valor=100000, porcentaje=0.18)

    [evento] = _eventos_de(contratacion["id"])
    assert evento.estado == "PENDIENTE"
    assert evento.tipo == "CONTRATACION_COMPLETADA"
    datos = json.loads(evento.payload)
    assert datos["eventoId"] == evento.id
    assert datos["prestadorId"] == contratacion["prestadorId"]
    assert datos["montoComision"] == 18000


def test_checkout_repetido_no_emite_un_segundo_evento():
    contratacion = _contratacion_completada()
    api_contrataciones.post(f"/contrataciones/{contratacion['id']}/check-out")
    assert len(_eventos_de(contratacion["id"])) == 1


def test_relay_publica_y_marca_publicado():
    contratacion = _contratacion_completada()
    publicador = PublicadorFalso()
    _publicar_todo(publicador)

    [evento] = _eventos_de(contratacion["id"])
    assert evento.estado == "PUBLICADO"
    assert evento.mensajeSnsId
    assert any(m["eventoId"] == evento.id for m in publicador.mensajes)


def test_con_sns_caido_el_evento_espera_y_sale_al_recuperarse():
    contratacion = _contratacion_completada()
    publicador = PublicadorFalso(caido=True)
    relay = RelayOutbox(contrataciones.Sesion, publicar=publicador)

    relay.publicar_pendientes()
    [evento] = _eventos_de(contratacion["id"])
    assert evento.estado == "PENDIENTE"
    assert evento.intentos == 1
    assert "LocalStack no responde" in evento.ultimoError

    publicador.caido = False
    while relay.publicar_pendientes():
        pass
    [evento] = _eventos_de(contratacion["id"])
    assert evento.estado == "PUBLICADO"


# --- Consumidor: worker SQS idempotente ------------------------------------

def test_el_consumidor_cobra_la_comision_en_la_billetera():
    contratacion = _contratacion_completada(valor=50000, porcentaje=0.12)
    publicador = PublicadorFalso()
    _publicar_todo(publicador)

    resultado = procesar_mensaje(monetizacion.Sesion, publicador.como_mensaje_sqs(), "sqs-1")

    assert resultado == "COMISION_COBRADA"
    assert _saldo(contratacion["prestadorId"]) == 6000
    cobro = api_monetizacion.get(f"/cobros/contratacion/{contratacion['id']}").json()
    assert cobro["cobrado"] is True
    assert cobro["evento"]["origen"] == "ASINCRONO_SQS"


def test_un_mensaje_duplicado_no_cobra_dos_veces():
    contratacion = _contratacion_completada(valor=80000, porcentaje=0.18)
    publicador = PublicadorFalso()
    _publicar_todo(publicador)
    mensaje = publicador.como_mensaje_sqs()

    assert procesar_mensaje(monetizacion.Sesion, mensaje, "sqs-a") == "COMISION_COBRADA"
    # SQS lo entrega de nuevo (at-least-once): mismo eventoId.
    assert procesar_mensaje(monetizacion.Sesion, mensaje, "sqs-b") == "DUPLICADO"
    assert _saldo(contratacion["prestadorId"]) == 14400


def test_un_evento_republicado_por_el_outbox_tampoco_cobra_dos_veces():
    """Si el relay publica dos veces (murió antes del commit), cambia el MessageId
    de SNS pero no el eventoId, que es lo que usa el consumidor."""
    contratacion = _contratacion_completada(valor=10000, porcentaje=0.18)
    publicador = PublicadorFalso()
    _publicar_todo(publicador)
    original = publicador.mensajes[-1]
    publicador.mensajes.append({**original, "snsId": str(uuid.uuid4())})

    procesar_mensaje(monetizacion.Sesion, publicador.como_mensaje_sqs(-2), "sqs-1")
    assert procesar_mensaje(monetizacion.Sesion, publicador.como_mensaje_sqs(-1), "sqs-2") == "DUPLICADO"
    assert _saldo(contratacion["prestadorId"]) == 1800


def test_acepta_el_mensaje_de_la_prueba_de_carga_sin_contratacion():
    """La prueba k6 de SNS publica eventos sin contratación real ni eventoId."""
    cuerpo = json.dumps({
        "Type": "Notification", "MessageId": str(uuid.uuid4()), "TopicArn": "arn:tema",
        "Message": json.dumps({"evento": "CONTRATACION_COMPLETADA", "monto": 45000,
                               "servicio_id": "SERV-K6-1-1"}),
    })
    assert procesar_mensaje(monetizacion.Sesion, cuerpo, "sqs-k6") == "REGISTRADO_SIN_CONTRATACION"
    cobros = api_monetizacion.get("/cobros").json()
    assert any(c["servicio_id"] == "SERV-K6-1-1" for c in cobros["cobros"])


def test_un_mensaje_ilegible_se_deja_para_reintento():
    """Lanzar hace que el worker no lo borre: SQS lo reentrega y luego va a la DLQ."""
    with pytest.raises(ValueError):
        procesar_mensaje(monetizacion.Sesion, "esto no es JSON", "sqs-roto")


def test_la_latencia_no_se_descuadra_con_fechas_en_utc():
    """k6 y la CLI mandan la hora en UTC ('...Z'); el servicio guarda hora local."""
    from datetime import datetime, timezone
    from monetizacion.tablas import EventoProcesadoFila

    sns_id = str(uuid.uuid4())
    cuerpo = json.dumps({
        "Type": "Notification", "MessageId": sns_id, "TopicArn": "arn:tema",
        "Message": json.dumps({"evento": "CONTRATACION_COMPLETADA", "monto": 1,
                               "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}),
    })
    procesar_mensaje(monetizacion.Sesion, cuerpo, "sqs-utc")
    with monetizacion.Sesion() as s:
        latencia = s.get(EventoProcesadoFila, sns_id).latenciaMs
    assert 0 <= latencia < 5000
