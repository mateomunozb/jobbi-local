"""Reglas de negocio RN-01 a RN-11: una clase de pruebas por regla.

Cada clase evidencia su regla por los dos lados: el caso que la cumple pasa y el
que la viola se rechaza. Los servicios corren en proceso (TestClient) sobre
SQLite temporal; SNS, SQS, el aliado externo y los demás contextos se sustituyen
por dobles, así que no hace falta Minikube ni LocalStack.

    cd services
    ../.venv/bin/python -m pytest -v tests/test_reglas_negocio.py            # todas
    ../.venv/bin/python -m pytest -v tests/test_reglas_negocio.py -k RN04    # una sola
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from adquisicion import main as adquisicion
from adquisicion.tablas import AliadoDistribucionFila, ReferidoFila
from common import eventos
from common.cobertura import MUNICIPIOS_VALLE_DE_ABURRA, en_cobertura
from common.enums import EstadoContratacion, EstadoVerificacion, PlanPrestador
from confianza import adaptador_verificacion as adaptador
from confianza import main as confianza
from confianza import verificacion
from contrataciones import main as contrataciones
from gateway import main as gateway
from identidad import main as identidad
from identidad import repositorio as repo_identidad
from identidad.models import PerfilDemandante, PerfilPrestador, Ubicacion
from monetizacion import main as monetizacion
from monetizacion import suscripciones
from monetizacion.comisiones import ESTRATEGIAS_POR_PLAN, ComisionCongelada, estrategia_de_cobro
from monetizacion.especificaciones import UMBRAL_BLOQUEO, EspecificacionPrestadorBloqueado
from monetizacion.sqs_worker import procesar_mensaje as procesar_en_monetizacion
from monetizacion.tablas import MovimientoBilleteraFila, SuscripcionProFila
from soporte import main as soporte

api_identidad = TestClient(identidad.app)
api_confianza = TestClient(confianza.app)
api_contrataciones = TestClient(contrataciones.app)
api_monetizacion = TestClient(monetizacion.app)
api_soporte = TestClient(soporte.app)

A, R, P = EstadoVerificacion.APROBADA, EstadoVerificacion.RECHAZADA, EstadoVerificacion.PENDIENTE


# --- Utilidades compartidas ---------------------------------------------------------

def _id() -> str:
    return str(uuid.uuid4())


def _registrar(rol: str = "Prestador", municipio: str = "Medellín", **extra):
    sufijo = uuid.uuid4().hex[:8]
    return api_identidad.post("/auth/registro", json={
        "nombreCompleto": f"{rol} Prueba", "correo": f"{rol.lower()}{sufijo}@prueba.co",
        "telefono": "3001234567", "numeroDocumento": f"9{sufijo[:6]}", "rol": rol,
        "municipio": municipio, **extra,
    })


def _prestador_registrado(**extra) -> dict:
    respuesta = _registrar("Prestador", **extra)
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()["perfilPrestador"]


def _veredicto(prestador_id: str, estado: str) -> dict:
    """Confianza le publica el veredicto a Identidad."""
    return api_identidad.post(f"/prestadores/{prestador_id}/verificacion", json={"estado": estado}).json()


def _gateway_lee(monkeypatch, respuestas: dict) -> list:
    """Doble de `_pedir`: responde por (servicio, ruta) y registra lo que se pidió."""
    pedidos = []

    async def _pedir(servicio, ruta, params=None):
        pedidos.append((servicio, ruta, params))
        return respuestas.get((servicio, ruta))
    monkeypatch.setattr(gateway, "_pedir", _pedir)
    return pedidos


def _gateway_escribe(monkeypatch) -> list:
    """Doble de `_enviar`: registra lo que el gateway reenvía y responde 201."""
    from fastapi.responses import JSONResponse
    enviados = []

    async def _enviar(servicio, ruta, cuerpo):
        enviados.append((servicio, ruta, cuerpo))
        return JSONResponse(status_code=201, content={**cuerpo, "id": _id(), "puntuacion": cuerpo.get("puntuacion")})

    async def _enviar_json(servicio, ruta, cuerpo):
        return None
    monkeypatch.setattr(gateway, "_enviar", _enviar)
    monkeypatch.setattr(gateway, "_enviar_json", _enviar_json)
    return enviados


def _contratacion(estado: str = "COMPLETADA", **extra) -> dict:
    return {"id": _id(), "estado": estado, "demandanteId": "dem-1", "prestadorId": "pres-1",
            "checkIn": None, "checkOut": None, **extra}


def _respuestas_ciclo(contratacion: dict, autores: tuple[str, ...] = ()) -> dict:
    return {
        ("contrataciones", f"/contrataciones/{contratacion['id']}"): contratacion,
        ("confianza", "/resenas"): {"total": len(autores), "items": [{"autorId": a} for a in autores]},
        ("soporte", "/incidentes"): {"total": 0, "items": []},
    }


def _completada_sqs(prestador_id: str, valor: float, porcentaje: float, medio: str = "EFECTIVO") -> tuple[str, str]:
    """Mensaje SQS de CONTRATACION_COMPLETADA tal como lo publica el outbox."""
    contratacion_id = _id()
    evento = {
        "eventoId": _id(), "tipo": eventos.CONTRATACION_COMPLETADA, "ocurridoEn": datetime.now().isoformat(),
        "contratacionId": contratacion_id, "prestadorId": prestador_id, "valorAcordado": valor,
        "medioPago": medio, "porcentajeComisionAplicado": porcentaje,
        "montoComision": round(valor * porcentaje, 2),
    }
    mensaje = json.dumps({"Type": "Notification", "MessageId": _id(), "TopicArn": "arn:tema",
                          "Message": json.dumps(evento)})
    return mensaje, contratacion_id


def _acordar(porcentaje: float, plan: str, medio: str = "EFECTIVO", valor: float = 100000) -> dict:
    """Propone y acepta un acuerdo en Contrataciones; devuelve la contratación que nace."""
    acuerdo = api_contrataciones.post("/acuerdos", json={
        "contactoId": _id(), "demandanteId": _id(), "prestadorId": _id(), "oficioId": _id(),
        "valorPropuesto": valor, "medioPago": medio, "propuestoPor": "DEMANDANTE",
    }).json()
    return api_contrataciones.post(f"/acuerdos/{acuerdo['id']}/aceptar", json={
        "rol": "PRESTADOR", "porcentajeComision": porcentaje, "planPrestador": plan,
    }).json()["contratacion"]


# =====================================================================================
class TestRN01InsigniaVerificadoCondicionada:
    """RN-01: insigniaVerificado = true solo con identidad Y antecedentes APROBADOS."""

    @pytest.mark.parametrize("identidad_, antecedentes, insignia", [
        (A, A, True),
        (A, R, False),
        (R, A, False),
        (A, P, False),
        (P, A, False),
        (R, R, False),
    ])
    def test_la_insignia_exige_ambas_verificaciones_aprobadas(self, identidad_, antecedentes, insignia):
        estados = {adaptador.IDENTIDAD: identidad_, adaptador.ANTECEDENTES: antecedentes}
        assert adaptador.insignia_vigente(estados) is insignia

    def test_con_una_sola_verificacion_no_hay_insignia(self):
        assert adaptador.insignia_vigente({adaptador.IDENTIDAD: A}) is False
        assert adaptador.insignia_vigente({adaptador.ANTECEDENTES: A}) is False

    @pytest.mark.parametrize("estados, global_", [
        ({adaptador.IDENTIDAD: A, adaptador.ANTECEDENTES: A}, "APROBADA"),
        ({adaptador.IDENTIDAD: A, adaptador.ANTECEDENTES: R}, "RECHAZADA"),
        ({adaptador.IDENTIDAD: A, adaptador.ANTECEDENTES: P}, "PENDIENTE"),
    ])
    def test_el_veredicto_global_solo_es_aprobado_con_las_dos(self, estados, global_):
        resultado = adaptador.ResultadoVerificacion(estados=estados, referenciaExterna="x", detalle="")
        assert verificacion.estado_de(resultado) == global_

    def test_el_prestador_nace_sin_insignia(self):
        perfil = _prestador_registrado()
        assert (perfil["insigniaVerificado"], perfil["estadoVerificacionActual"]) == (False, "PENDIENTE")

    @pytest.mark.parametrize("estado, insignia", [("APROBADA", True), ("PENDIENTE", False), ("RECHAZADA", False)])
    def test_la_insignia_del_perfil_sigue_al_veredicto(self, estado, insignia):
        perfil = _veredicto(_prestador_registrado()["id"], estado)
        assert (perfil["estadoVerificacionActual"], perfil["insigniaVerificado"]) == (estado, insignia)

    def test_sin_insignia_no_se_publica_en_el_buscador(self, monkeypatch):
        pedidos = _gateway_lee(monkeypatch, {})
        asyncio.run(gateway.bff_catalogo(categoriaId=None, calificacionMinima=None, municipio=None))
        [filtros] = [params for servicio, ruta, params in pedidos if (servicio, ruta) == ("identidad", "/prestadores")]
        assert filtros["estadoVerificacion"] == "APROBADA"


# =====================================================================================
class TestRN02ResenaSoloTrasContratacionCompletada:
    """RN-02: solo puede existir una Resena si la Contratacion está COMPLETADA."""

    @pytest.mark.parametrize("estado", ["SOLICITADA", "ACEPTADA", "EN_CURSO", "CHECK_IN", "CHECK_OUT",
                                        "CANCELADA", "EN_DISPUTA"])
    def test_sin_completar_no_se_ofrece_calificar(self, estado):
        ciclo = gateway._ciclo_desde(_contratacion(estado), {"total": 0, "items": []}, {"total": 0, "items": []})
        assert ciclo["puedeCalificar"] == {"DEMANDANTE": False, "PRESTADOR": False}

    def test_completada_admite_calificar(self):
        ciclo = gateway._ciclo_desde(_contratacion("COMPLETADA"), {"total": 0, "items": []}, {"total": 0, "items": []})
        assert ciclo["puedeCalificar"] == {"DEMANDANTE": True, "PRESTADOR": True}

    @pytest.mark.parametrize("estado", ["ACEPTADA", "CHECK_IN", "CANCELADA"])
    def test_el_gateway_rechaza_la_resena_de_un_servicio_no_completado(self, monkeypatch, estado):
        contratacion = _contratacion(estado)
        _gateway_lee(monkeypatch, _respuestas_ciclo(contratacion))
        enviados = _gateway_escribe(monkeypatch)
        with pytest.raises(HTTPException) as error:
            asyncio.run(gateway.bff_resena({"contratacionId": contratacion["id"], "autorId": "dem-1",
                                            "puntuacion": 5}))
        assert error.value.status_code == 409
        assert enviados == []                        # la reseña nunca llega a Confianza

    def test_el_gateway_acepta_la_resena_de_un_servicio_completado(self, monkeypatch):
        contratacion = _contratacion("COMPLETADA")
        _gateway_lee(monkeypatch, _respuestas_ciclo(contratacion))
        enviados = _gateway_escribe(monkeypatch)
        asyncio.run(gateway.bff_resena({"contratacionId": contratacion["id"], "autorId": "pres-1",
                                        "puntuacion": 5}))
        assert [(s, r) for s, r, _ in enviados] == [("confianza", "/resenas")]

    def test_una_contratacion_inexistente_no_admite_resena(self, monkeypatch):
        _gateway_lee(monkeypatch, {})
        with pytest.raises(HTTPException) as error:
            asyncio.run(gateway.bff_resena({"contratacionId": "no-existe", "autorId": "dem-1", "puntuacion": 5}))
        assert error.value.status_code == 404


# =====================================================================================
class TestRN03ResenaBidireccionalYUnica:
    """RN-03: máximo 2 reseñas por contratación, una por dirección."""

    def _resena(self, contratacion_id: str, autor: str, receptor: str, puntuacion: int = 5):
        return api_confianza.post("/resenas", json={"contratacionId": contratacion_id, "autorId": autor,
                                                    "receptorId": receptor, "puntuacion": puntuacion})

    def test_una_por_direccion_y_nunca_dos_en_la_misma(self):
        contratacion, demandante, prestador = _id(), _id(), _id()
        assert self._resena(contratacion, demandante, prestador).status_code == 201   # demandante → prestador
        assert self._resena(contratacion, prestador, demandante).status_code == 201   # prestador → demandante
        assert self._resena(contratacion, demandante, prestador, 1).status_code == 409
        assert self._resena(contratacion, prestador, demandante, 1).status_code == 409

        guardadas = api_confianza.get("/resenas", params={"contratacionId": contratacion}).json()
        assert guardadas["total"] == 2
        assert sorted(r["puntuacion"] for r in guardadas["items"]) == [5, 5]   # el segundo intento no pisó nada

    def test_la_segunda_resena_repetida_no_distorsiona_el_promedio(self):
        contratacion, demandante, prestador = _id(), _id(), _id()
        self._resena(contratacion, demandante, prestador, 5)
        self._resena(contratacion, demandante, prestador, 1)                  # rechazada
        resumen = api_confianza.get("/resenas/resumen", params={"receptorId": prestador}).json()
        assert (resumen["total"], resumen["promedio"]) == (1, 5)

    def test_tras_calificar_ambas_partes_nadie_puede_calificar(self):
        ciclo = gateway._ciclo_desde(_contratacion("COMPLETADA"),
                                     {"total": 2, "items": [{"autorId": "dem-1"}, {"autorId": "pres-1"}]},
                                     {"total": 0, "items": []})
        assert ciclo["puedeCalificar"] == {"DEMANDANTE": False, "PRESTADOR": False}

    def test_un_tercero_no_puede_agregar_una_tercera_resena(self, monkeypatch):
        contratacion = _contratacion("COMPLETADA")
        _gateway_lee(monkeypatch, _respuestas_ciclo(contratacion))
        enviados = _gateway_escribe(monkeypatch)
        with pytest.raises(HTTPException) as error:
            asyncio.run(gateway.bff_resena({"contratacionId": contratacion["id"], "autorId": "intruso",
                                            "puntuacion": 1}))
        assert error.value.status_code == 403
        assert enviados == []

    def test_la_direccion_sale_de_la_contratacion_no_del_cliente(self, monkeypatch):
        contratacion = _contratacion("COMPLETADA")
        _gateway_lee(monkeypatch, _respuestas_ciclo(contratacion))
        enviados = _gateway_escribe(monkeypatch)
        asyncio.run(gateway.bff_resena({"contratacionId": contratacion["id"], "autorId": "pres-1",
                                        "receptorId": "otra-persona", "puntuacion": 4}))
        [(_, _, cuerpo)] = enviados
        assert cuerpo["receptorId"] == "dem-1"

    def test_el_gateway_no_deja_repetir_la_misma_direccion(self, monkeypatch):
        contratacion = _contratacion("COMPLETADA")
        _gateway_lee(monkeypatch, _respuestas_ciclo(contratacion, autores=("pres-1",)))
        enviados = _gateway_escribe(monkeypatch)
        with pytest.raises(HTTPException) as error:
            asyncio.run(gateway.bff_resena({"contratacionId": contratacion["id"], "autorId": "pres-1",
                                            "puntuacion": 1}))
        assert error.value.status_code == 409
        assert enviados == []


# =====================================================================================
class TestRN04ComisionPendienteYBloqueoPorEfectivo:
    """RN-04: pago en EFECTIVO → cargo de comisión en la billetera; sobre el umbral, bloqueada."""

    def _movimientos(self, contratacion_id: str) -> list[MovimientoBilleteraFila]:
        with monetizacion.Sesion() as s:
            return s.query(MovimientoBilleteraFila).filter_by(contratacionId=contratacion_id).all()

    def _billetera(self, prestador_id: str) -> dict:
        return api_monetizacion.get(f"/resumen/prestador/{prestador_id}").json()["billetera"]

    def test_el_efectivo_genera_el_cargo_de_la_comision(self):
        prestador = _id()
        mensaje, contratacion = _completada_sqs(prestador, 100000, 0.18)
        assert procesar_en_monetizacion(monetizacion.Sesion, mensaje, "m-1") == "COMISION_COBRADA"
        [cargo] = self._movimientos(contratacion)
        assert cargo.tipo == "COMISION" and abs(cargo.monto) == 18000
        assert self._billetera(prestador)["saldoPendiente"] == 18000

    def test_el_pago_por_plataforma_no_genera_cargo(self):
        prestador = _id()
        mensaje, contratacion = _completada_sqs(prestador, 100000, 0.18, medio="PLATAFORMA")
        assert procesar_en_monetizacion(monetizacion.Sesion, mensaje, "m-2") == "SIN_COBRO_MEDIO_PLATAFORMA"
        assert self._movimientos(contratacion) == []

    def test_el_cargo_se_genera_una_sola_vez_aunque_el_evento_se_repita(self):
        prestador = _id()
        mensaje, contratacion = _completada_sqs(prestador, 100000, 0.18)
        procesar_en_monetizacion(monetizacion.Sesion, mensaje, "m-3")
        procesar_en_monetizacion(monetizacion.Sesion, mensaje, "m-3-reentrega")
        assert len(self._movimientos(contratacion)) == 1
        assert self._billetera(prestador)["saldoPendiente"] == 18000

    @pytest.mark.parametrize("saldo, bloqueada", [
        (UMBRAL_BLOQUEO - 1, False), (UMBRAL_BLOQUEO, True), (UMBRAL_BLOQUEO + 1, True),
    ])
    def test_la_especificacion_de_bloqueo(self, saldo, bloqueada):
        assert EspecificacionPrestadorBloqueado().es_satisfecha_por(type("B", (), {"saldoPendiente": saldo})) \
            is bloqueada

    def test_acumular_comisiones_hasta_el_umbral_bloquea_la_billetera(self):
        prestador = _id()
        # 18 % de 500.000 = 90.000 por servicio: el segundo lleva el saldo a 180.000 > 150.000.
        procesar_en_monetizacion(monetizacion.Sesion, _completada_sqs(prestador, 500000, 0.18)[0], "m-4")
        assert self._billetera(prestador)["bloqueada"] is False
        procesar_en_monetizacion(monetizacion.Sesion, _completada_sqs(prestador, 500000, 0.18)[0], "m-5")
        billetera = self._billetera(prestador)
        assert (billetera["saldoPendiente"], billetera["bloqueada"]) == (180000, True)

    def test_con_la_billetera_bloqueada_no_se_acuerdan_servicios(self, monkeypatch):
        _gateway_lee(monkeypatch, {("monetizacion", "/billeteras"): {
            "items": [{"bloqueada": True, "saldoPendiente": UMBRAL_BLOQUEO}]}})
        with pytest.raises(HTTPException) as error:
            asyncio.run(gateway._exigir_billetera_al_dia("pres-1"))
        assert error.value.status_code == 409


# =====================================================================================
class TestRN05CongelamientoDelPorcentajeDeComision:
    """RN-05: porcentajeComisionAplicado se fija al crear la contratación y no cambia."""

    @pytest.mark.parametrize("plan, porcentaje, comision", [("PRO", 0.12, 12000), ("FREE", 0.18, 18000)])
    def test_la_contratacion_nace_con_el_porcentaje_del_plan_vigente(self, plan, porcentaje, comision):
        contratacion = _acordar(porcentaje, plan)
        assert (contratacion["porcentajeComisionAplicado"], contratacion["montoComision"]) == (porcentaje, comision)

    def test_el_porcentaje_no_cambia_durante_el_ciclo_de_vida(self):
        contratacion = _acordar(0.12, "PRO")
        api_contrataciones.post(f"/contrataciones/{contratacion['id']}/check-in")
        cerrada = api_contrataciones.post(f"/contrataciones/{contratacion['id']}/check-out").json()
        assert cerrada["estado"] == "COMPLETADA"
        assert (cerrada["porcentajeComisionAplicado"], cerrada["montoComision"]) == (0.12, 12000)

    def test_cambiar_de_plan_despues_no_toca_lo_ya_acordado(self):
        perfil = _prestador_registrado()
        api_identidad.post(f"/prestadores/{perfil['id']}/plan", json={"plan": "PRO"})
        contratacion = _acordar(0.12, "PRO")
        api_identidad.post(f"/prestadores/{perfil['id']}/plan", json={"plan": "FREE"})   # baja a FREE
        guardada = api_contrataciones.get(f"/contrataciones/{contratacion['id']}").json()
        assert guardada["porcentajeComisionAplicado"] == 0.12

    def test_el_cobro_usa_el_porcentaje_congelado_y_no_el_plan_de_hoy(self):
        estrategia = estrategia_de_cobro({"porcentajeComisionAplicado": 0.12, "planPrestador": "FREE"})
        assert isinstance(estrategia, ComisionCongelada)
        assert estrategia.calcular(100000) == 12000

    def test_monetizacion_cobra_lo_congelado(self):
        prestador = _id()
        mensaje, _ = _completada_sqs(prestador, 100000, 0.12)
        procesar_en_monetizacion(monetizacion.Sesion, mensaje, "m-6")
        assert api_monetizacion.get(f"/resumen/prestador/{prestador}").json()["billetera"]["saldoPendiente"] == 12000


# =====================================================================================
class TestRN06UnaSolaSuscripcionProActiva:
    """RN-06: una sola SuscripcionPro ACTIVA; al vencer sin renovar, vuelve a FREE (18 %)."""

    def _activas(self, prestador_id: str) -> list[dict]:
        return api_monetizacion.get("/suscripciones", params={"prestadorId": prestador_id,
                                                              "estado": "ACTIVA"}).json()["items"]

    def _suscripcion_vencida(self, prestador_id: str) -> None:
        with monetizacion.Sesion() as s:
            s.add(SuscripcionProFila(id=_id(), prestadorId=prestador_id, fechaInicio=date.today() - timedelta(days=40),
                                     fechaRenovacion=date.today() - timedelta(days=10), estado="ACTIVA",
                                     valorMensual=39900.0))
            s.commit()

    def test_activar_pro_dos_veces_deja_una_sola_activa(self):
        prestador = _id()
        primera = api_monetizacion.post("/suscripciones", json={"prestadorId": prestador, "plan": "PRO"}).json()
        segunda = api_monetizacion.post("/suscripciones", json={"prestadorId": prestador, "plan": "PRO"}).json()
        assert primera["suscripcion"]["id"] == segunda["suscripcion"]["id"]
        assert len(self._activas(prestador)) == 1

    def test_pasar_a_free_cancela_la_activa(self):
        prestador = _id()
        api_monetizacion.post("/suscripciones", json={"prestadorId": prestador, "plan": "PRO"})
        respuesta = api_monetizacion.post("/suscripciones", json={"prestadorId": prestador, "plan": "FREE"}).json()
        assert (respuesta["plan"], respuesta["porcentajeComision"]) == ("FREE", 0.18)
        assert self._activas(prestador) == []

    def test_al_vencer_sin_renovar_vuelve_a_free_con_18_por_ciento(self):
        prestador = _id()
        self._suscripcion_vencida(prestador)
        antes = api_monetizacion.get(f"/prestadores/{prestador}/plan-vigente").json()
        assert (antes["plan"], antes["porcentajeComision"]) == ("FREE", 0.18)     # ya no rige aunque diga ACTIVA

        resultado = api_monetizacion.post("/suscripciones/vencer").json()
        assert prestador in resultado["prestadores"]
        assert self._activas(prestador) == []
        vencidas = api_monetizacion.get("/suscripciones", params={"prestadorId": prestador,
                                                                  "estado": "VENCIDA"}).json()["items"]
        assert len(vencidas) == 1

    def test_renovar_tras_vencer_abre_un_periodo_nuevo_y_sigue_habiendo_una_activa(self):
        prestador = _id()
        self._suscripcion_vencida(prestador)
        nueva = api_monetizacion.post("/suscripciones", json={"prestadorId": prestador, "plan": "PRO"}).json()
        assert date.fromisoformat(nueva["suscripcion"]["fechaRenovacion"]) > date.today()
        assert len(self._activas(prestador)) == 1

    @pytest.mark.parametrize("estado, renovacion, plan", [
        ("ACTIVA", date.today() + timedelta(days=1), PlanPrestador.PRO),
        ("ACTIVA", date.today(), PlanPrestador.PRO),                       # vence al terminar el día
        ("ACTIVA", date.today() - timedelta(days=1), PlanPrestador.FREE),
        ("CANCELADA", date.today() + timedelta(days=10), PlanPrestador.FREE),
    ])
    def test_plan_vigente_segun_la_suscripcion(self, estado, renovacion, plan):
        fila = SuscripcionProFila(estado=estado, fechaRenovacion=renovacion)
        assert suscripciones.plan_vigente(fila, date.today()) is plan
        assert suscripciones.plan_vigente(None, date.today()) is PlanPrestador.FREE

    def test_las_tarifas_son_12_pro_y_18_free(self):
        assert ESTRATEGIAS_POR_PLAN["PRO"].porcentaje == 0.12
        assert ESTRATEGIAS_POR_PLAN["FREE"].porcentaje == 0.18


# =====================================================================================
class TestRN07VerificacionRechazadaBloqueaPublicacion:
    """RN-07: si la verificación más reciente es RECHAZADA, el perfil no aparece en la búsqueda."""

    def _visibles(self) -> set[str]:
        items = api_identidad.get("/prestadores", params={"estadoVerificacion": "APROBADA", "size": 100}).json()
        return {p["id"] for p in items["items"]}

    def test_un_rechazado_no_aparece_hasta_una_nueva_aprobacion(self):
        prestador = _prestador_registrado()["id"]
        _veredicto(prestador, "APROBADA")
        assert prestador in self._visibles()

        _veredicto(prestador, "RECHAZADA")                 # la verificación más reciente manda
        assert prestador not in self._visibles()

        _veredicto(prestador, "APROBADA")                  # nueva verificación aprobada
        assert prestador in self._visibles()

    def test_el_rechazo_quita_la_insignia(self):
        prestador = _prestador_registrado()["id"]
        _veredicto(prestador, "APROBADA")
        perfil = _veredicto(prestador, "RECHAZADA")
        assert (perfil["estadoVerificacionActual"], perfil["insigniaVerificado"]) == ("RECHAZADA", False)

    def test_el_buscador_del_gateway_solo_pide_aprobados(self, monkeypatch):
        pedidos = _gateway_lee(monkeypatch, {})
        asyncio.run(gateway.bff_catalogo(categoriaId=None, calificacionMinima=None, municipio="Medellín"))
        [filtros] = [params for servicio, ruta, params in pedidos if (servicio, ruta) == ("identidad", "/prestadores")]
        assert filtros["estadoVerificacion"] == "APROBADA"

    def test_un_rechazado_tampoco_puede_acordar_servicios(self, monkeypatch):
        _gateway_lee(monkeypatch, {("identidad", "/prestadores/pres-1"): {"estadoVerificacionActual": "RECHAZADA"}})
        with pytest.raises(HTTPException) as error:
            asyncio.run(gateway._exigir_prestador_verificado("pres-1"))
        assert error.value.status_code == 409

    def test_confianza_publica_el_rechazo_a_identidad(self, monkeypatch):
        publicados = []

        def publicar(sujeto):
            publicados.append((sujeto.prestadorId, sujeto.estado))
            sujeto.sincronizado = True
            return True
        monkeypatch.setattr(verificacion, "publicar_en_identidad", publicar)
        monkeypatch.setattr(adaptador, "verificar", lambda prestador_id, documento: adaptador.ResultadoVerificacion(
            estados={adaptador.IDENTIDAD: A, adaptador.ANTECEDENTES: R}, referenciaExterna="x", detalle=""))
        prestador = _id()
        api_confianza.post(f"/prestadores/{prestador}/verificacion", json={"documento": "1017000000"})
        assert publicados == [(prestador, "RECHAZADA")]


# =====================================================================================
class TestRN08UnPerfilDeCadaTipoPorUsuario:
    """RN-08: un Usuario tiene a lo sumo un PerfilDemandante y un PerfilPrestador."""

    def _ubicacion(self) -> Ubicacion:
        return Ubicacion(municipio="Medellín", comuna="", barrio="", latitud=0, longitud=0)

    def _perfil_prestador(self, usuario_id: str) -> PerfilPrestador:
        return PerfilPrestador(
            id=_id(), usuarioId=usuario_id, descripcion="", portafolioUrl="", tarifaReferencialBase=0,
            insigniaVerificado=False, estadoVerificacionActual=P, planActual=PlanPrestador.FREE,
            calificacionPromedio=0, totalResenas=0, fechaActivacion=date.today(), ubicacionPrincipal=self._ubicacion())

    def test_el_mismo_correo_no_crea_una_segunda_identidad(self):
        primera = _registrar("Prestador")
        correo = primera.json()["usuario"]["correo"]
        for rol in ("Prestador", "Demandante"):
            repetida = api_identidad.post("/auth/registro", json={
                "nombreCompleto": "Otra Persona", "correo": correo.upper(), "telefono": "3000000000",
                "numeroDocumento": "12345678", "rol": rol})
            assert repetida.status_code == 409

    def test_no_se_crea_un_segundo_perfil_de_prestador(self):
        usuario = _registrar("Prestador").json()["usuario"]["id"]
        with repo_identidad.abrir() as s:
            with pytest.raises(repo_identidad.PerfilDuplicado):
                repo_identidad.crear_prestador(s, self._perfil_prestador(usuario))

    def test_no_se_crea_un_segundo_perfil_de_demandante(self):
        usuario = _registrar("Demandante").json()["usuario"]["id"]
        with repo_identidad.abrir() as s:
            with pytest.raises(repo_identidad.PerfilDuplicado):
                repo_identidad.crear_demandante(s, PerfilDemandante(
                    id=_id(), usuarioId=usuario, ubicacionPrincipal=self._ubicacion(), fechaActivacion=date.today()))

    def test_si_puede_tener_uno_de_cada_tipo(self):
        usuario = _registrar("Prestador").json()["usuario"]["id"]
        with repo_identidad.abrir() as s:
            repo_identidad.crear_demandante(s, PerfilDemandante(
                id=_id(), usuarioId=usuario, ubicacionPrincipal=self._ubicacion(), fechaActivacion=date.today()))
            s.commit()
            assert repo_identidad.prestador_de(s, usuario) and repo_identidad.demandante_de(s, usuario)


# =====================================================================================
class TestRN09RestriccionGeograficaFase1:
    """RN-09: perfiles y contrataciones solo en municipios del Valle de Aburrá."""

    @pytest.mark.parametrize("municipio", [*MUNICIPIOS_VALLE_DE_ABURRA, "MEDELLIN", "itagui", " la  estrella "])
    def test_los_municipios_del_valle_de_aburra_tienen_cobertura(self, municipio):
        assert en_cobertura(municipio) is True

    @pytest.mark.parametrize("municipio", ["Rionegro", "Bogotá", "Cali", "Guarne", "", None])
    def test_fuera_del_valle_de_aburra_no_hay_cobertura(self, municipio):
        assert en_cobertura(municipio) is False

    @pytest.mark.parametrize("rol", ["Prestador", "Demandante"])
    def test_no_se_registra_un_perfil_fuera_de_cobertura(self, rol):
        respuesta = _registrar(rol, municipio="Rionegro")
        assert respuesta.status_code == 422
        assert "Valle de Aburrá" in respuesta.text

    @pytest.mark.parametrize("rol, municipio", [("Prestador", "Envigado"), ("Demandante", "Bello")])
    def test_si_se_registra_dentro_de_cobertura(self, rol, municipio):
        respuesta = _registrar(rol, municipio=municipio)
        assert respuesta.status_code == 201

    def _perfiles(self, municipio_dem: str, municipio_pres: str) -> dict:
        return {
            ("identidad", "/demandantes/dem-1"): {"ubicacionPrincipal": {"municipio": municipio_dem}},
            ("identidad", "/prestadores/pres-1"): {"ubicacionPrincipal": {"municipio": municipio_pres}},
        }

    @pytest.mark.parametrize("municipio_dem, municipio_pres", [("Rionegro", "Medellín"), ("Medellín", "Rionegro")])
    def test_no_se_contrata_si_una_parte_esta_fuera(self, monkeypatch, municipio_dem, municipio_pres):
        _gateway_lee(monkeypatch, self._perfiles(municipio_dem, municipio_pres))
        with pytest.raises(HTTPException) as error:
            asyncio.run(gateway._exigir_cobertura("dem-1", "pres-1"))
        assert error.value.status_code == 409
        assert "RN-09" in error.value.detail

    def test_si_se_contrata_con_ambas_partes_dentro(self, monkeypatch):
        _gateway_lee(monkeypatch, self._perfiles("Sabaneta", "Itagüí"))
        asyncio.run(gateway._exigir_cobertura("dem-1", "pres-1"))

    def test_proponer_un_acuerdo_aplica_la_regla(self, monkeypatch):
        respuestas = self._perfiles("Medellín", "Rionegro")
        respuestas[("identidad", "/prestadores/pres-1")]["estadoVerificacionActual"] = "APROBADA"
        _gateway_lee(monkeypatch, respuestas)
        with pytest.raises(HTTPException) as error:
            asyncio.run(gateway.bff_proponer_acuerdo({"contactoId": "c-1", "demandanteId": "dem-1",
                                                      "prestadorId": "pres-1"}))
        assert error.value.status_code == 409


# =====================================================================================
class TestRN10EvidenciaObligatoriaParaInvestigar:
    """RN-10: ABIERTO → EN_INVESTIGACION solo con check-in/check-out registrado o denuncia formal."""

    def _incidente(self) -> dict:
        return api_soporte.post("/incidentes", json={"contratacionId": _id(), "tipo": "DANO",
                                                     "evidenciaDescripcion": "Rompió la puerta"}).json()

    def _investigar(self, incidente_id: str, **evidencia):
        return api_soporte.post(f"/incidentes/{incidente_id}/investigar", json=evidencia)

    def test_sin_evidencia_se_queda_abierto(self):
        incidente = self._incidente()
        respuesta = self._investigar(incidente["id"])
        assert respuesta.status_code == 409
        assert "RN-10" in respuesta.json()["detail"]
        assert api_soporte.get(f"/incidentes/{incidente['id']}").json()["estado"] == "ABIERTO"

    def test_una_denuncia_en_blanco_no_cuenta_como_evidencia(self):
        assert self._investigar(self._incidente()["id"], denunciaFormal="   ").status_code == 409

    @pytest.mark.parametrize("evidencia", [
        {"checkInRegistrado": True},
        {"checkOutRegistrado": True},
        {"denunciaFormal": "Denuncia N.º 123 ante la Fiscalía"},
    ])
    def test_con_evidencia_pasa_a_investigacion(self, evidencia):
        incidente = self._incidente()
        respuesta = self._investigar(incidente["id"], **evidencia)
        assert respuesta.status_code == 200
        assert respuesta.json()["estado"] == "EN_INVESTIGACION"

    def test_la_denuncia_queda_documentada_en_el_incidente(self):
        incidente = self._incidente()
        investigado = self._investigar(incidente["id"], denunciaFormal="Denuncia N.º 123").json()
        assert "Denuncia N.º 123" in investigado["evidenciaDescripcion"]

    def test_solo_un_incidente_abierto_pasa_a_investigacion(self):
        incidente = self._incidente()
        self._investigar(incidente["id"], checkInRegistrado=True)
        assert self._investigar(incidente["id"], checkInRegistrado=True).status_code == 409

    @pytest.mark.parametrize("check_in, check_out, investiga", [
        (None, None, False), ("2026-09-25T10:00:00", None, True), ("2026-09-25T10:00:00", "2026-09-25T12:00:00", True),
    ])
    def test_el_gateway_toma_la_evidencia_de_la_contratacion(self, monkeypatch, check_in, check_out, investiga):
        incidente = self._incidente()
        contratacion = {"id": incidente["contratacionId"], "checkIn": check_in, "checkOut": check_out}
        _gateway_lee(monkeypatch, {
            ("soporte", f"/incidentes/{incidente['id']}"): incidente,
            ("contrataciones", f"/contrataciones/{incidente['contratacionId']}"): contratacion,
        })

        async def _enviar(servicio, ruta, cuerpo):      # reenvía a Soporte de verdad
            respuesta = api_soporte.post(ruta, json=cuerpo)
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=respuesta.status_code, content=respuesta.json())
        monkeypatch.setattr(gateway, "_enviar", _enviar)

        respuesta = asyncio.run(gateway.bff_investigar_incidente(incidente["id"], {}))
        assert (respuesta.status_code == 200) is investiga
        estado = api_soporte.get(f"/incidentes/{incidente['id']}").json()["estado"]
        assert estado == ("EN_INVESTIGACION" if investiga else "ABIERTO")


# =====================================================================================
class TestRN11LaReferenciaDeUnAliadoNoEximeLaVerificacion:
    """RN-11: un prestador referido por un AliadoDistribucion pasa por la misma verificación."""

    def _referir(self, prestador_id: str) -> None:
        with adquisicion.Sesion() as s:
            aliado = AliadoDistribucionFila(id=_id(), nombre="Conjunto Residencial El Poblado",
                                            tipo="ADMINISTRACION_CONJUNTO", zonaCobertura="Medellín")
            s.add(aliado)
            s.flush()
            s.add(ReferidoFila(aliadoId=aliado.id, prestadorId=prestador_id,
                               fechaReferencia=date.today(), activado=True))
            s.commit()

    def test_un_referido_nace_sin_insignia_y_pendiente(self):
        perfil = _prestador_registrado()
        self._referir(perfil["id"])
        guardado = api_identidad.get(f"/prestadores/{perfil['id']}").json()
        assert (guardado["insigniaVerificado"], guardado["estadoVerificacionActual"]) == (False, "PENDIENTE")
        assert api_adquisicion_referidos(perfil["id"]) == 1

    def test_el_registro_ignora_cualquier_intento_de_traer_la_insignia(self):
        perfil = _prestador_registrado(aliadoId=_id(), insigniaVerificado=True, estadoVerificacionActual="APROBADA")
        assert (perfil["insigniaVerificado"], perfil["estadoVerificacionActual"]) == (False, "PENDIENTE")

    def test_adquisicion_no_tiene_como_otorgar_la_insignia(self):
        escrituras = {(r.path, m) for r in adquisicion.app.routes for m in getattr(r, "methods", set())
                      if m in ("POST", "PUT", "PATCH", "DELETE")}
        assert escrituras == set()

    def test_un_referido_con_antecedentes_rechazados_no_obtiene_insignia(self, monkeypatch):
        perfil = _prestador_registrado()
        self._referir(perfil["id"])
        monkeypatch.setattr(adaptador, "verificar", lambda prestador_id, documento: adaptador.ResultadoVerificacion(
            estados={adaptador.IDENTIDAD: A, adaptador.ANTECEDENTES: R}, referenciaExterna="x", detalle=""))
        monkeypatch.setattr(verificacion, "publicar_en_identidad",
                            lambda sujeto: _veredicto(sujeto.prestadorId, sujeto.estado) and True)
        api_confianza.post(f"/prestadores/{perfil['id']}/verificacion", json={"documento": "1017999999"})
        guardado = api_identidad.get(f"/prestadores/{perfil['id']}").json()
        assert (guardado["insigniaVerificado"], guardado["estadoVerificacionActual"]) == (False, "RECHAZADA")

    def test_un_referido_obtiene_la_insignia_solo_por_la_verificacion(self, monkeypatch):
        perfil = _prestador_registrado()
        self._referir(perfil["id"])
        monkeypatch.setattr(adaptador, "verificar", lambda prestador_id, documento: adaptador.ResultadoVerificacion(
            estados={adaptador.IDENTIDAD: A, adaptador.ANTECEDENTES: A}, referenciaExterna="x", detalle=""))
        monkeypatch.setattr(verificacion, "publicar_en_identidad",
                            lambda sujeto: _veredicto(sujeto.prestadorId, sujeto.estado) and True)
        api_confianza.post(f"/prestadores/{perfil['id']}/verificacion", json={"documento": "1017888888"})
        assert api_identidad.get(f"/prestadores/{perfil['id']}").json()["insigniaVerificado"] is True


def api_adquisicion_referidos(prestador_id: str) -> int:
    return len(TestClient(adquisicion.app).get("/referidos", params={"prestadorId": prestador_id}).json())
