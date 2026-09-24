"""Chat por servicio y en tiempo real (Comunicación).

Un contacto tiene a lo sumo un chat ABIERTO; al cerrarse su servicio el chat
queda de solo lectura y contactar de nuevo abre otro. Los mensajes viajan por
WebSocket a todos los conectados a la sala.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from common.db import completar_columnas
from comunicacion import main as comunicacion


@pytest.fixture()
def api():
    # Con `with`, peticiones HTTP y WebSockets comparten el mismo event loop, que
    # es lo que permite a un POST difundir a una sala abierta por WebSocket.
    with TestClient(comunicacion.app) as cliente:
        yield cliente


def _chat(api, contacto: str) -> dict:
    return api.post("/conversaciones", json={"contactoId": contacto}).json()


def test_contactar_con_un_chat_abierto_reusa_el_mismo(api):
    contacto = str(uuid.uuid4())
    primero = _chat(api, contacto)
    assert primero["estado"] == "ABIERTA"
    assert _chat(api, contacto)["id"] == primero["id"]


def test_cerrado_el_servicio_contactar_abre_un_chat_nuevo(api):
    contacto = str(uuid.uuid4())
    viejo = _chat(api, contacto)
    cerrado = api.post(f"/conversaciones/{viejo['id']}/cerrar", json={"contratacionId": "c-1"}).json()
    assert cerrado["estado"] == "CERRADA" and cerrado["contratacionId"] == "c-1"

    nuevo = _chat(api, contacto)
    assert nuevo["id"] != viejo["id"] and nuevo["estado"] == "ABIERTA"

    lista = api.get("/conversaciones", params={"contactoId": contacto}).json()
    assert {c["estado"] for c in lista["items"]} == {"ABIERTA", "CERRADA"}


def test_un_chat_cerrado_no_admite_mensajes(api):
    chat = _chat(api, str(uuid.uuid4()))
    api.post(f"/conversaciones/{chat['id']}/cerrar", json={})
    respuesta = api.post("/mensajes", json={"conversacionId": chat["id"], "remitenteId": "u1", "contenido": "hola"})
    assert respuesta.status_code == 409


def test_el_mensaje_llega_en_vivo_a_toda_la_sala(api):
    chat = _chat(api, str(uuid.uuid4()))
    ruta = f"/ws/conversaciones/{chat['id']}"
    with api.websocket_connect(f"{ruta}?usuarioId=demandante") as dem, \
         api.websocket_connect(f"{ruta}?usuarioId=prestador") as pres:
        assert dem.receive_json()["tipo"] == "conectado"
        assert dem.receive_json() == {"tipo": "presencia", "enLinea": 2}  # llegó el prestador
        assert pres.receive_json()["enLinea"] == 2

        # "Escribiendo…" solo lo ve la otra parte.
        dem.send_json({"tipo": "escribiendo"})
        assert pres.receive_json() == {"tipo": "escribiendo", "usuarioId": "demandante"}

        dem.send_json({"tipo": "mensaje", "contenido": "¿Puedes venir hoy?"})
        for ws in (dem, pres):  # a quien lo envió le sirve de confirmación
            evento = ws.receive_json()
            assert evento["tipo"] == "mensaje"
            assert evento["mensaje"]["contenido"] == "¿Puedes venir hoy?"
            assert evento["mensaje"]["remitenteId"] == "demandante"

        # Un mensaje enviado por HTTP también llega en vivo.
        api.post("/mensajes", json={"conversacionId": chat["id"], "remitenteId": "prestador", "contenido": "Sí"})
        assert dem.receive_json()["mensaje"]["contenido"] == "Sí"
        assert pres.receive_json()["mensaje"]["contenido"] == "Sí"

    guardados = api.get(f"/conversaciones/{chat['id']}/mensajes").json()
    assert [m["contenido"] for m in guardados["items"]] == ["¿Puedes venir hoy?", "Sí"]


def test_al_cerrarse_el_chat_ambos_lo_ven_y_ya_no_pueden_escribir(api):
    chat = _chat(api, str(uuid.uuid4()))
    ruta = f"/ws/conversaciones/{chat['id']}"
    with api.websocket_connect(f"{ruta}?usuarioId=demandante") as dem, \
         api.websocket_connect(f"{ruta}?usuarioId=prestador") as pres:
        dem.receive_json(); dem.receive_json(); pres.receive_json()

        api.post(f"/conversaciones/{chat['id']}/cerrar", json={"contratacionId": "c-9"})
        for ws in (dem, pres):
            evento = ws.receive_json()
            assert evento["tipo"] == "cerrada" and evento["conversacion"]["estado"] == "CERRADA"

        pres.send_json({"tipo": "mensaje", "contenido": "¿Hola?"})
        assert pres.receive_json() == {
            "tipo": "error", "detalle": "Este chat se cerró al terminar el servicio", "codigo": 409,
        }


def test_una_conversacion_inexistente_cierra_con_4404(api):
    """Se acepta y se cierra con 4404 (no un 403 en el handshake), para que el
    navegador reciba el código y no se quede reintentando."""
    from starlette.websockets import WebSocketDisconnect
    with api.websocket_connect("/ws/conversaciones/no-existe?usuarioId=u1") as ws:
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
    assert error.value.code == 4404


def test_completar_columnas_agrega_las_nuevas_sin_tocar_los_datos(tmp_path):
    """Una base creada antes del chat por servicio gana las columnas nuevas."""
    motor = create_engine(f"sqlite:///{tmp_path / 'vieja.db'}")
    with motor.begin() as c:
        c.execute(text('CREATE TABLE conversaciones (id VARCHAR(36) PRIMARY KEY, '
                       '"contactoId" VARCHAR(36), "fechaInicio" DATE)'))
        c.execute(text("INSERT INTO conversaciones VALUES ('c1', 'k1', '2026-09-01')"))

    completar_columnas(motor)
    completar_columnas(motor)  # correr de nuevo no hace nada

    columnas = {col["name"] for col in inspect(motor).get_columns("conversaciones")}
    assert {"estado", "creadaEn", "fechaCierre", "contratacionId"} <= columnas
    with motor.connect() as c:
        assert c.execute(text('SELECT id, estado FROM conversaciones')).all() == [("c1", "ABIERTA")]
