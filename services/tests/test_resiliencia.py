"""Resiliencia: Circuit Breaker, backoff exponencial y Adapter del aliado (RN-01).

    cd services && ../.venv/bin/python -m pytest -v tests/test_resiliencia.py
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from common.enums import EstadoVerificacion
from common.resiliencia import ABIERTO, CERRADO, SEMIABIERTO, Backoff, CircuitBreaker, CircuitoAbierto, es_error_de_base
from confianza import adaptador_verificacion as adaptador
from confianza import main as confianza
from confianza import verificacion
from gateway import main as gateway
from identidad import main as identidad_main
from verificacion_externa import main as aliado

A, R, P = EstadoVerificacion.APROBADA, EstadoVerificacion.RECHAZADA, EstadoVerificacion.PENDIENTE


class Reloj:
    def __init__(self) -> None:
        self.ahora = 0.0

    def __call__(self) -> float:
        return self.ahora


def _falla():
    raise httpx.ReadTimeout("el aliado tardó 8 s")


# --- Circuit Breaker ------------------------------------------------------------

def test_el_circuito_se_abre_tras_n_fallos_y_deja_de_llamar():
    reloj, llamadas = Reloj(), []
    circuito = CircuitBreaker("prueba-1", umbral_fallos=3, espera_segundos=30, reloj=reloj)

    for _ in range(3):
        with pytest.raises(httpx.ReadTimeout):
            circuito.llamar(_falla)
    assert circuito.estado == ABIERTO

    # Abierto: responde al instante sin tocar la dependencia.
    with pytest.raises(CircuitoAbierto):
        circuito.llamar(lambda: llamadas.append(1))
    assert llamadas == []


def test_tras_la_espera_una_prueba_exitosa_lo_cierra():
    reloj = Reloj()
    circuito = CircuitBreaker("prueba-2", umbral_fallos=1, espera_segundos=30, reloj=reloj)
    with pytest.raises(httpx.ReadTimeout):
        circuito.llamar(_falla)

    reloj.ahora = 31
    assert circuito.estado == SEMIABIERTO
    assert circuito.llamar(lambda: "ok") == "ok"
    assert circuito.estado == CERRADO


def test_una_prueba_fallida_en_semiabierto_lo_reabre():
    reloj = Reloj()
    circuito = CircuitBreaker("prueba-3", umbral_fallos=1, espera_segundos=30, reloj=reloj)
    with pytest.raises(httpx.ReadTimeout):
        circuito.llamar(_falla)
    reloj.ahora = 31
    with pytest.raises(httpx.ReadTimeout):
        circuito.llamar(_falla)
    assert circuito.estado == ABIERTO


def test_un_exito_reinicia_la_cuenta_de_fallos():
    circuito = CircuitBreaker("prueba-4", umbral_fallos=3, reloj=Reloj())
    for _ in range(2):
        with pytest.raises(httpx.ReadTimeout):
            circuito.llamar(_falla)
    circuito.llamar(lambda: None)
    for _ in range(2):
        with pytest.raises(httpx.ReadTimeout):
            circuito.llamar(_falla)
    assert circuito.estado == CERRADO


# --- Backoff exponencial ------------------------------------------------------------

def test_el_backoff_crece_exponencialmente_hasta_el_tope():
    backoff = Backoff(inicial=1, maximo=30)
    esperas = [backoff.siguiente() for _ in range(8)]
    for base, espera in zip([1, 2, 4, 8, 16, 30, 30, 30], esperas):
        assert base * 0.8 <= espera <= base * 1.2
    backoff.reiniciar()
    assert backoff.siguiente() <= 1.2


def test_solo_los_errores_de_base_pausan_a_los_consumidores():
    assert es_error_de_base(OperationalError("SELECT 1", {}, Exception("connection refused")))
    assert not es_error_de_base(ValueError("mensaje ilegible"))


# --- Adapter (Anti-Corruption Layer) y RN-01 --------------------------------------

def _respuesta_aliado(identidad: str = "valid", antecedentes: str = "clear", status: str = "completed") -> dict:
    return {"check_id": "CHK1", "status": status, "score": 0.9,
            "summary": {"identity": {"result": identidad}, "background": {"result": antecedentes}}}


def test_el_adapter_traduce_el_formato_del_aliado_al_dominio():
    resultado = adaptador.traducir(_respuesta_aliado())
    assert resultado.estados == {adaptador.IDENTIDAD: A, adaptador.ANTECEDENTES: A}
    assert resultado.referenciaExterna == "CHK1"


def test_lo_que_el_aliado_no_confirma_queda_pendiente():
    assert set(adaptador.traducir(_respuesta_aliado(status="in_progress")).estados.values()) == {P}
    assert adaptador.traducir(_respuesta_aliado(antecedentes="???")).estados[adaptador.ANTECEDENTES] == P


@pytest.mark.parametrize("identidad, antecedentes, insignia", [
    (A, A, True),
    (A, R, False),   # antecedentes con hallazgos
    (R, A, False),   # identidad no válida
    (A, P, False),   # falta el resultado de antecedentes
])
def test_rn01_la_insignia_exige_identidad_y_antecedentes_aprobados(identidad, antecedentes, insignia):
    estados = {adaptador.IDENTIDAD: identidad, adaptador.ANTECEDENTES: antecedentes}
    assert adaptador.insignia_vigente(estados) is insignia


# --- Confianza: verificación al registrarse, PENDIENTE y reverificador (RN-01) ------

api_confianza = TestClient(confianza.app)


@pytest.fixture
def identidad(monkeypatch):
    """Doble de Identidad: registra lo que Confianza le publica."""
    publicados = []

    def publicar(sujeto):
        publicados.append((sujeto.prestadorId, sujeto.estado))
        sujeto.sincronizado = True
        return True
    monkeypatch.setattr(verificacion, "publicar_en_identidad", publicar)
    return publicados


def _aliado_responde(monkeypatch, antecedentes="clear", llamadas=None):
    def verificar(prestador_id, documento):
        if llamadas is not None:
            llamadas.append(documento)
        return adaptador.traducir(_respuesta_aliado(antecedentes=antecedentes))
    monkeypatch.setattr(adaptador, "verificar", verificar)


def _aliado_falla(monkeypatch, error):
    def verificar(prestador_id, documento):
        raise error
    monkeypatch.setattr(adaptador, "verificar", verificar)


def _solicitar(prestador, documento="1017123456"):
    return api_confianza.post(f"/prestadores/{prestador}/verificacion", json={"documento": documento})


def test_al_registrarse_con_el_aliado_sano_queda_aprobado(monkeypatch, identidad):
    prestador = str(uuid.uuid4())
    _aliado_responde(monkeypatch)
    datos = _solicitar(prestador).json()
    assert (datos["estado"], datos["origen"], datos["degradado"]) == ("APROBADA", "ALIADO", False)
    assert identidad == [(prestador, "APROBADA")]


def test_con_antecedentes_con_hallazgos_queda_rechazado(monkeypatch, identidad):
    prestador = str(uuid.uuid4())
    _aliado_responde(monkeypatch, antecedentes="found")
    assert _solicitar(prestador).json()["estado"] == "RECHAZADA"
    assert identidad == [(prestador, "RECHAZADA")]


def test_con_el_aliado_caido_queda_pendiente_y_el_reverificador_lo_resuelve(monkeypatch, identidad):
    prestador = str(uuid.uuid4())
    _aliado_falla(monkeypatch, httpx.ReadTimeout("8 s"))
    datos = _solicitar(prestador).json()
    assert (datos["estado"], datos["degradado"]) == ("PENDIENTE", True)
    assert identidad == []                       # Identidad ya lo tiene PENDIENTE

    _aliado_responde(monkeypatch)               # el aliado vuelve
    verificacion.reverificar(confianza.Sesion)
    assert (prestador, "APROBADA") in identidad


def test_con_el_circuito_abierto_queda_pendiente_al_instante(monkeypatch, identidad):
    _aliado_falla(monkeypatch, CircuitoAbierto("abierto"))
    datos = _solicitar(str(uuid.uuid4())).json()
    assert (datos["estado"], datos["origen"]) == ("PENDIENTE", "CIRCUITO_ABIERTO")


def test_el_reverificador_no_llama_al_aliado_con_el_circuito_abierto(monkeypatch, identidad):
    _aliado_falla(monkeypatch, CircuitoAbierto("abierto"))
    _solicitar(str(uuid.uuid4()))
    monkeypatch.setattr(verificacion.adaptador, "circuito", SimpleNamespace(estado=ABIERTO))
    _aliado_falla(monkeypatch, AssertionError("no debería llamar al aliado"))
    verificacion.reverificar(confianza.Sesion)


def test_con_veredicto_vigente_no_se_reconsulta(monkeypatch, identidad):
    prestador, llamadas = str(uuid.uuid4()), []
    _aliado_responde(monkeypatch, llamadas=llamadas)
    _solicitar(prestador)
    assert api_confianza.post(f"/prestadores/{prestador}/verificacion").json()["origen"] == "VIGENTE"
    assert llamadas == ["1017123456"]             # una sola consulta pagada, con el documento


def test_un_aprobado_vencido_conserva_su_estado_si_el_aliado_cae(monkeypatch, identidad):
    prestador = str(uuid.uuid4())
    _aliado_responde(monkeypatch)
    _solicitar(prestador)
    monkeypatch.setattr(verificacion, "VIGENCIA_DIAS", 0)   # el veredicto de hoy ya venció
    _aliado_falla(monkeypatch, httpx.ReadTimeout("8 s"))
    datos = api_confianza.post(f"/prestadores/{prestador}/verificacion").json()
    assert (datos["estado"], datos["degradado"]) == ("APROBADA", True)


def test_la_primera_verificacion_exige_el_documento():
    assert api_confianza.post(f"/prestadores/{uuid.uuid4()}/verificacion").status_code == 422


def test_cada_consulta_al_aliado_se_cobra_una_sola_vez(monkeypatch, identidad):
    prestador = str(uuid.uuid4())
    _aliado_responde(monkeypatch)
    _solicitar(prestador)
    # Una consulta trae identidad y antecedentes: cuesta 3.500, no 7.000.
    assert api_confianza.get(f"/prestadores/{prestador}/estado-verificacion").json()["costoAcumulado"] == 3500


# --- Identidad: el prestador nace PENDIENTE y la insignia sigue al veredicto --------

def test_el_prestador_nace_pendiente_y_sin_insignia():
    api = TestClient(identidad_main.app)
    sufijo = uuid.uuid4().hex[:8]
    sesion = api.post("/auth/registro", json={
        "nombreCompleto": "Prestador Prueba", "correo": f"p{sufijo}@prueba.co", "telefono": "3001234567",
        "numeroDocumento": f"9{sufijo[:6]}", "rol": "Prestador"}).json()
    perfil = sesion["perfilPrestador"]
    assert (perfil["estadoVerificacionActual"], perfil["insigniaVerificado"], sesion["verificado"]) == (
        "PENDIENTE", False, False)

    aprobado = api.post(f"/prestadores/{perfil['id']}/verificacion", json={"estado": "APROBADA"}).json()
    assert (aprobado["estadoVerificacionActual"], aprobado["insigniaVerificado"]) == ("APROBADA", True)


# --- Gateway: solo un prestador APROBADO acuerda servicios ---------------------------

def _identidad_responde(perfil):
    async def _pedir(servicio, ruta, params=None):
        assert servicio == "identidad"
        return perfil
    return _pedir


@pytest.mark.parametrize("estado", ["PENDIENTE", "RECHAZADA"])
def test_el_gateway_no_deja_acordar_con_un_prestador_no_aprobado(monkeypatch, estado):
    monkeypatch.setattr(gateway, "_pedir", _identidad_responde({"estadoVerificacionActual": estado}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(gateway._exigir_prestador_verificado("pres-1"))
    assert error.value.status_code == 409


@pytest.mark.parametrize("perfil", [{"estadoVerificacionActual": "APROBADA"}, None])
def test_el_gateway_deja_acordar_con_un_aprobado_o_si_identidad_no_responde(monkeypatch, perfil):
    monkeypatch.setattr(gateway, "_pedir", _identidad_responde(perfil))
    asyncio.run(gateway._exigir_prestador_verificado("pres-1"))


# --- Simulador del aliado -------------------------------------------------------------------

def test_el_simulador_inyecta_y_retira_el_fallo():
    api = TestClient(aliado.app)
    assert api.post("/v1/checks", json={"user_reference": "x"}).json()["status"] == "completed"
    api.post("/_caos", json={"codigo": 504})
    assert api.post("/v1/checks", json={"user_reference": "x"}).status_code == 504
    api.delete("/_caos")
    assert api.post("/v1/checks", json={"user_reference": "x"}).status_code == 201


def test_el_aliado_decide_por_documento_de_forma_reproducible():
    documentos = [str(10**9 + i) for i in range(1000)]
    rechazados = [d for d in documentos if aliado.con_hallazgos(d)]
    assert 150 <= len(rechazados) <= 250                      # ~20 %
    assert all(aliado.con_hallazgos(d) for d in rechazados)   # mismo documento, mismo veredicto
    api = TestClient(aliado.app)
    respuesta = api.post("/v1/checks", json={"national_id": rechazados[0]}).json()
    assert respuesta["summary"]["background"]["result"] == "found"
