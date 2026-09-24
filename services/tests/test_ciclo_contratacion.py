"""Ciclo de vida de una contratación: terminar, calificar, reportar y cerrar.

Un servicio terminado admite calificar (una vez por parte) y reportar un
incidente (uno). Cuando el demandante lo califica queda cerrado: ya no admite
nada de eso y el contacto queda libre para acordar un servicio nuevo.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from confianza import main as confianza
from contrataciones import main as contrataciones
from gateway.main import _ciclo_desde
from soporte import main as soporte

api_contrataciones = TestClient(contrataciones.app)
api_confianza = TestClient(confianza.app)
api_soporte = TestClient(soporte.app)

DEMANDANTE, PRESTADOR = "dem-1", "pres-1"


def _contratacion(estado: str) -> dict:
    return {"id": "c-1", "estado": estado, "demandanteId": DEMANDANTE, "prestadorId": PRESTADOR}


def _resenas(*autores: str) -> dict:
    return {"total": len(autores), "items": [{"autorId": a} for a in autores]}


SIN_INCIDENTES = {"total": 0, "items": []}


# --- Regla del gateway (función pura) ---------------------------------------

def test_en_curso_no_se_califica_pero_si_se_reporta():
    ciclo = _ciclo_desde(_contratacion("CHECK_IN"), _resenas(), SIN_INCIDENTES)
    assert ciclo["puedeCalificar"] == {"DEMANDANTE": False, "PRESTADOR": False}
    assert ciclo["puedeReportarIncidente"] is True
    assert ciclo["cerrada"] is False


def test_terminado_sin_calificar_admite_calificar_y_reportar():
    ciclo = _ciclo_desde(_contratacion("COMPLETADA"), _resenas(), SIN_INCIDENTES)
    assert ciclo["puedeCalificar"] == {"DEMANDANTE": True, "PRESTADOR": True}
    assert ciclo["puedeReportarIncidente"] is True
    assert ciclo["cerrada"] is False


def test_calificado_por_el_demandante_queda_cerrado():
    ciclo = _ciclo_desde(_contratacion("COMPLETADA"), _resenas(DEMANDANTE), SIN_INCIDENTES)
    assert ciclo["cerrada"] is True
    assert ciclo["puedeCalificar"]["DEMANDANTE"] is False
    assert ciclo["puedeReportarIncidente"] is False
    # El prestador aún puede calificar a su cliente: su reseña no reabre nada.
    assert ciclo["puedeCalificar"]["PRESTADOR"] is True


def test_la_resena_del_prestador_no_cierra_el_servicio():
    ciclo = _ciclo_desde(_contratacion("COMPLETADA"), _resenas(PRESTADOR), SIN_INCIDENTES)
    assert ciclo["cerrada"] is False
    assert ciclo["puedeCalificar"] == {"DEMANDANTE": True, "PRESTADOR": False}


def test_con_un_incidente_no_se_reporta_otro():
    ciclo = _ciclo_desde(_contratacion("COMPLETADA"), _resenas(), {"total": 1, "items": [{}]})
    assert ciclo["incidenteReportado"] is True
    assert ciclo["puedeReportarIncidente"] is False


def test_sin_respuesta_de_confianza_no_se_ofrece_calificar():
    ciclo = _ciclo_desde(_contratacion("COMPLETADA"), None, SIN_INCIDENTES)
    assert ciclo["puedeCalificar"] == {"DEMANDANTE": False, "PRESTADOR": False}
    assert ciclo["puedeReportarIncidente"] is False


# --- Reglas que cada servicio aplica por su cuenta -------------------------

def test_confianza_no_admite_calificar_dos_veces():
    resena = {"contratacionId": str(uuid.uuid4()), "autorId": DEMANDANTE,
              "receptorId": PRESTADOR, "puntuacion": 5, "comentario": "Excelente"}
    assert api_confianza.post("/resenas", json=resena).status_code == 201
    segunda = api_confianza.post("/resenas", json={**resena, "puntuacion": 1})
    assert segunda.status_code == 409

    guardada = api_confianza.get("/resenas", params={"contratacionId": resena["contratacionId"]}).json()
    assert [r["puntuacion"] for r in guardada["items"]] == [5]


def test_soporte_no_admite_un_segundo_incidente():
    reporte = {"contratacionId": str(uuid.uuid4()), "tipo": "RETRASO", "evidenciaDescripcion": "Llegó tarde"}
    assert api_soporte.post("/incidentes", json=reporte).status_code == 201
    assert api_soporte.post("/incidentes", json=reporte).status_code == 409


def test_no_se_acuerda_otro_servicio_con_uno_en_curso():
    contacto = str(uuid.uuid4())
    base = {"contactoId": contacto, "demandanteId": str(uuid.uuid4()), "prestadorId": str(uuid.uuid4()),
            "oficioId": str(uuid.uuid4()), "valorPropuesto": 50000, "medioPago": "EFECTIVO",
            "propuestoPor": "DEMANDANTE"}
    acuerdo = api_contrataciones.post("/acuerdos", json=base).json()
    servicio = api_contrataciones.post(f"/acuerdos/{acuerdo['id']}/aceptar",
                                       json={"rol": "PRESTADOR"}).json()["contratacion"]

    # En curso: no se puede negociar otro.
    assert api_contrataciones.post("/acuerdos", json=base).status_code == 409

    # Terminado: Contrataciones ya lo permite (que esté calificado lo exige el gateway).
    api_contrataciones.post(f"/contrataciones/{servicio['id']}/check-in")
    api_contrataciones.post(f"/contrataciones/{servicio['id']}/check-out")
    assert api_contrataciones.post("/acuerdos", json=base).status_code == 201
