"""Reglas del caso de uso: cobro en efectivo, bloqueo de la billetera y su aviso.

Cubren los patrones del catálogo que viven en este flujo: Specification (RN-04),
Strategy (RN-05/06), State (RN-02), outbox de Monetización y consumidor
idempotente de Comunicación, con dobles en lugar de SNS, SQS e Identidad.

    cd services && ../.venv/bin/python -m pytest -v tests/test_billetera_y_patrones.py
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from common import eventos
from comunicacion import main as comunicacion
from comunicacion.sqs_worker import procesar_mensaje as procesar_en_comunicacion
from comunicacion.tablas import NotificacionFila
from contrataciones import main as contrataciones
from contrataciones.tablas import ContratacionFila
from gateway import main as gateway
from monetizacion import main as monetizacion
from monetizacion.billetera import aplicar_comision
from monetizacion.repositorio import RepositorioBilleteras
from monetizacion.comisiones import (
    ComisionCongelada,
    ComisionPlanFree,
    ComisionPlanPro,
    estrategia_de_cobro,
)
from monetizacion.especificaciones import UMBRAL_BLOQUEO, EspecificacionPrestadorBloqueado
from monetizacion.sqs_worker import procesar_mensaje as procesar_en_monetizacion
from monetizacion.tablas import EventoOutboxMonetizacionFila, PagoFila

api_contrataciones = TestClient(contrataciones.app)


def _eventos_de_bloqueo(billetera_id: str) -> list[EventoOutboxMonetizacionFila]:
    with monetizacion.Sesion() as s:
        return s.query(EventoOutboxMonetizacionFila).filter_by(agregadoId=billetera_id).all()


def _cobrar(prestador_id: str, monto: float):
    with monetizacion.Sesion() as s:
        resultado = aplicar_comision(RepositorioBilleteras(s), prestador_id, str(uuid.uuid4()), monto)
        s.commit()
        return resultado.billetera


def _completada(prestador_id: str, valor: float, porcentaje: float, medio: str = "EFECTIVO") -> str:
    """Mensaje SQS de CONTRATACION_COMPLETADA, tal como lo arma el outbox."""
    evento = {
        "eventoId": str(uuid.uuid4()), "tipo": eventos.CONTRATACION_COMPLETADA,
        "ocurridoEn": datetime.now().isoformat(),
        "contratacionId": str(uuid.uuid4()), "prestadorId": prestador_id,
        "valorAcordado": valor, "medioPago": medio,
        "porcentajeComisionAplicado": porcentaje, "montoComision": round(valor * porcentaje, 2),
    }
    return json.dumps({"Type": "Notification", "MessageId": str(uuid.uuid4()),
                       "TopicArn": "arn:tema", "Message": json.dumps(evento)})


# --- Specification: bloqueo por saldo pendiente (RN-04) ---------------------

@pytest.mark.parametrize("saldo, bloquea", [
    (UMBRAL_BLOQUEO - 1, False),
    (UMBRAL_BLOQUEO, True),       # el umbral exacto ya bloquea
    (UMBRAL_BLOQUEO + 1, True),
])
def test_la_especificacion_bloquea_desde_el_umbral_exacto(saldo, bloquea):
    billetera = SimpleNamespace(saldoPendiente=saldo)
    assert EspecificacionPrestadorBloqueado().es_satisfecha_por(billetera) is bloquea


def test_alcanzar_el_umbral_bloquea_y_emite_un_solo_aviso():
    prestador = str(uuid.uuid4())
    billetera = _cobrar(prestador, UMBRAL_BLOQUEO - 1)
    assert billetera.bloqueada is False
    assert _eventos_de_bloqueo(billetera.id) == []

    billetera = _cobrar(prestador, 1)
    assert billetera.bloqueada is True
    [evento] = _eventos_de_bloqueo(billetera.id)
    assert evento.tipo == eventos.BILLETERA_BLOQUEADA
    assert evento.estado == "PENDIENTE"
    datos = json.loads(evento.payload)
    assert datos["prestadorId"] == prestador
    assert datos["saldoPendiente"] == UMBRAL_BLOQUEO

    # Seguir cobrando a una billetera ya bloqueada no repite el aviso.
    _cobrar(prestador, 5000)
    assert len(_eventos_de_bloqueo(billetera.id)) == 1


# --- Strategy: comisión congelada al contratar (RN-05, RN-06) ---------------

def test_las_tarifas_por_plan_son_las_del_catalogo():
    assert ComisionPlanFree().calcular(100000) == 18000
    assert ComisionPlanPro().calcular(100000) == 12000


def test_cambiar_de_plan_despues_de_contratar_no_cambia_la_comision():
    """Contrató en Pro (12 %) y pasó a Free antes de terminar: se cobra el 12 %."""
    evento = {"porcentajeComisionAplicado": 0.12, "planPrestador": "FREE", "valorAcordado": 100000}
    estrategia = estrategia_de_cobro(evento)
    assert isinstance(estrategia, ComisionCongelada)
    assert estrategia.calcular(100000) == 12000


def test_sin_porcentaje_congelado_se_usa_la_tarifa_del_plan_o_ninguna():
    assert isinstance(estrategia_de_cobro({"planPrestador": "pro"}), ComisionPlanPro)
    assert estrategia_de_cobro({"monto": 1}) is None


def test_el_consumidor_cobra_con_la_estrategia_congelada():
    prestador = str(uuid.uuid4())
    assert procesar_en_monetizacion(monetizacion.Sesion, _completada(prestador, 50000, 0.12), "m-1") \
        == "COMISION_COBRADA"
    resumen = TestClient(monetizacion.app).get(f"/resumen/prestador/{prestador}").json()
    assert resumen["billetera"]["saldoPendiente"] == 6000


# --- Pago en efectivo registrado --------------------------------------------

def test_el_cobro_en_efectivo_deja_registrado_el_pago_una_sola_vez():
    mensaje = _completada(str(uuid.uuid4()), 80000, 0.18)
    contratacion_id = json.loads(json.loads(mensaje)["Message"])["contratacionId"]

    procesar_en_monetizacion(monetizacion.Sesion, mensaje, "p-1")
    procesar_en_monetizacion(monetizacion.Sesion, mensaje, "p-2")  # reentrega de SQS

    with monetizacion.Sesion() as s:
        [pago] = s.query(PagoFila).filter_by(contratacionId=contratacion_id).all()
    assert pago.monto == 80000
    assert pago.referenciaPasarela == "EFECTIVO"


def test_con_pago_por_plataforma_no_se_registra_pago_en_efectivo():
    mensaje = _completada(str(uuid.uuid4()), 80000, 0.18, medio="PLATAFORMA")
    contratacion_id = json.loads(json.loads(mensaje)["Message"])["contratacionId"]
    assert procesar_en_monetizacion(monetizacion.Sesion, mensaje, "p-3") == "SIN_COBRO_MEDIO_PLATAFORMA"
    with monetizacion.Sesion() as s:
        assert s.query(PagoFila).filter_by(contratacionId=contratacion_id).count() == 0


# --- Comunicación: consumidor idempotente del aviso de bloqueo --------------

def _aviso_de_bloqueo(prestador_id: str) -> str:
    evento = {"eventoId": str(uuid.uuid4()), "tipo": eventos.BILLETERA_BLOQUEADA,
              "prestadorId": prestador_id, "saldoPendiente": 150000, "umbralBloqueo": 150000}
    return json.dumps({"Type": "Notification", "MessageId": str(uuid.uuid4()),
                       "TopicArn": "arn:tema", "Message": json.dumps(evento)})


def _notificaciones_de(usuario_id: str) -> list[NotificacionFila]:
    with comunicacion.Sesion() as s:
        return s.query(NotificacionFila).filter_by(usuarioId=usuario_id).all()


def test_el_bloqueo_notifica_al_usuario_del_prestador_una_sola_vez():
    usuario = str(uuid.uuid4())
    mensaje = _aviso_de_bloqueo("pres-1")
    resolver = lambda _prestador: usuario  # noqa: E731 — doble de Identidad

    assert procesar_en_comunicacion(comunicacion.Sesion, mensaje, "c-1", resolver) == "NOTIFICADO"
    assert procesar_en_comunicacion(comunicacion.Sesion, mensaje, "c-2", resolver) == "DUPLICADO"

    [notificacion] = _notificaciones_de(usuario)
    assert notificacion.tipo == eventos.BILLETERA_BLOQUEADA
    assert "150,000" in notificacion.contenido


def test_sin_identidad_el_aviso_se_deja_para_reintento():
    """Lanzar hace que el worker no borre el mensaje: SQS lo reentrega y luego va a la DLQ."""
    def identidad_caida(_prestador: str) -> str:
        raise ConnectionError("Identidad no responde")

    mensaje = _aviso_de_bloqueo("pres-2")
    with pytest.raises(ConnectionError):
        procesar_en_comunicacion(comunicacion.Sesion, mensaje, "c-3", identidad_caida)
    # No quedó marcado como recibido: el reintento sí notificará.
    assert procesar_en_comunicacion(comunicacion.Sesion, mensaje, "c-4", lambda _: "u-2") == "NOTIFICADO"


# --- State: transiciones de la contratación (RN-02) -------------------------

def _contratacion_aceptada() -> str:
    acuerdo = api_contrataciones.post("/acuerdos", json={
        "contactoId": str(uuid.uuid4()), "demandanteId": str(uuid.uuid4()),
        "prestadorId": str(uuid.uuid4()), "oficioId": str(uuid.uuid4()),
        "valorPropuesto": 60000, "medioPago": "EFECTIVO", "propuestoPor": "DEMANDANTE",
    }).json()
    return api_contrataciones.post(f"/acuerdos/{acuerdo['id']}/aceptar",
                                   json={"rol": "PRESTADOR"}).json()["contratacion"]["id"]


def test_no_se_cierra_un_servicio_sin_check_in():
    contratacion_id = _contratacion_aceptada()
    respuesta = api_contrataciones.post(f"/contrataciones/{contratacion_id}/check-out")
    assert respuesta.status_code == 409
    assert "check-in" in respuesta.json()["detail"]


def test_una_contratacion_cancelada_no_se_puede_cerrar():
    contratacion_id = _contratacion_aceptada()
    api_contrataciones.post(f"/contrataciones/{contratacion_id}/check-in")
    with contrataciones.Sesion() as s:
        s.get(ContratacionFila, contratacion_id).estado = "CANCELADA"
        s.commit()

    respuesta = api_contrataciones.post(f"/contrataciones/{contratacion_id}/check-out")
    assert respuesta.status_code == 409
    assert "CANCELADA" in respuesta.json()["detail"]


def test_el_flujo_valido_llega_a_completada():
    contratacion_id = _contratacion_aceptada()
    assert api_contrataciones.post(f"/contrataciones/{contratacion_id}/check-in").json()["estado"] == "CHECK_IN"
    assert api_contrataciones.post(f"/contrataciones/{contratacion_id}/check-out").json()["estado"] == "COMPLETADA"


# --- Gateway: la billetera bloqueada impide acordar servicios nuevos --------

def _monetizacion_responde(respuesta):
    async def _pedir(servicio, ruta, params=None):
        assert servicio == "monetizacion"
        return respuesta
    return _pedir


def test_el_gateway_rechaza_acuerdos_de_un_prestador_bloqueado(monkeypatch):
    monkeypatch.setattr(gateway, "_pedir", _monetizacion_responde(
        {"items": [{"bloqueada": True, "saldoPendiente": 160000}]}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(gateway._exigir_billetera_al_dia("pres-1"))
    assert error.value.status_code == 409


@pytest.mark.parametrize("respuesta", [
    {"items": [{"bloqueada": False, "saldoPendiente": 1000}]},
    {"items": []},   # nunca ha trabajado: no tiene billetera
    None,            # Monetización caída: no se paraliza el mercado
])
def test_el_gateway_deja_pasar_si_no_hay_bloqueo(monkeypatch, respuesta):
    monkeypatch.setattr(gateway, "_pedir", _monetizacion_responde(respuesta))
    asyncio.run(gateway._exigir_billetera_al_dia("pres-1"))
