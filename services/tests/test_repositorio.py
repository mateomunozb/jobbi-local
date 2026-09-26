"""Repository: las reglas del caso de uso corren sin base de datos.

La regla del cobro (`aplicar_comision`) y el ciclo de vida (`estados`) se
ejecutan aquí contra un repositorio en memoria y un modelo del dominio puro: si
el Repository no aislara el SQL, estas pruebas no podrían existir.

    cd services && ../.venv/bin/python -m pytest -v tests/test_repositorio.py
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest

from common import eventos
from common.enums import EstadoContratacion, MedioPago
from contrataciones.estados import TransicionInvalida, estado_de
from contrataciones.models import Contratacion
from monetizacion.billetera import UMBRAL_BLOQUEO, aplicar_comision, registrar_pago_efectivo
from monetizacion.models import BilleteraPrestador, MovimientoBilletera, Pago


class BilleterasEnMemoria:
    """Doble de RepositorioBilleteras: mismo contrato, sin SQL."""

    def __init__(self) -> None:
        self.billeteras: dict[str, BilleteraPrestador] = {}
        self.cargos: list[MovimientoBilletera] = []
        self.pagos: list[Pago] = []
        self.eventos: list[tuple[str, str, dict[str, Any]]] = []

    def obtener_o_crear(self, prestador_id: str) -> BilleteraPrestador:
        if prestador_id not in self.billeteras:
            self.billeteras[prestador_id] = BilleteraPrestador(
                id=str(uuid.uuid4()), prestadorId=prestador_id, saldoPendiente=0, bloqueada=False)
        return self.billeteras[prestador_id]

    def _por_id(self, billetera_id: str) -> BilleteraPrestador:
        return next(b for b in self.billeteras.values() if b.id == billetera_id)

    def cargo_de(self, billetera_id, contratacion_id):
        return next((m for m in self.cargos
                     if m.billeteraId == billetera_id and m.contratacionId == contratacion_id), None)

    def registrar_cargo(self, billetera_id, contratacion_id, monto):
        movimiento = MovimientoBilletera(id=str(uuid.uuid4()), billeteraId=billetera_id,
                                         contratacionId=contratacion_id, tipo="COMISION",
                                         monto=-monto, fecha=date.today())
        self.cargos.append(movimiento)
        billetera = self._por_id(billetera_id)
        billetera.saldoPendiente += monto
        return movimiento, billetera.model_copy()

    def fijar_bloqueo(self, billetera_id, bloqueada):
        self._por_id(billetera_id).bloqueada = bloqueada

    def pago_de(self, contratacion_id):
        return next((p for p in self.pagos if p.contratacionId == contratacion_id), None)

    def agregar_pago(self, pago):
        self.pagos.append(pago)

    def registrar_evento(self, tipo, agregado_id, datos):
        self.eventos.append((tipo, agregado_id, datos))


# --- Regla del cobro (Monetización) -------------------------------------------------

def test_el_cobro_carga_la_comision_sin_base_de_datos():
    repo = BilleterasEnMemoria()
    resultado = aplicar_comision(repo, "pres-1", "c-1", 18000)
    assert resultado.yaCobrada is False
    assert resultado.billetera.saldoPendiente == 18000
    assert repo.cargos[0].monto == -18000


def test_el_cobro_es_idempotente_por_contratacion():
    repo = BilleterasEnMemoria()
    aplicar_comision(repo, "pres-1", "c-1", 18000)
    segundo = aplicar_comision(repo, "pres-1", "c-1", 18000)
    assert segundo.yaCobrada is True
    assert len(repo.cargos) == 1 and repo.billeteras["pres-1"].saldoPendiente == 18000


def test_cruzar_el_umbral_bloquea_y_deja_un_solo_evento():
    repo = BilleterasEnMemoria()
    aplicar_comision(repo, "pres-1", "c-1", UMBRAL_BLOQUEO - 1)
    assert repo.eventos == []
    resultado = aplicar_comision(repo, "pres-1", "c-2", 1)
    assert resultado.billetera.bloqueada is True and repo.billeteras["pres-1"].bloqueada is True
    assert [tipo for tipo, _, _ in repo.eventos] == [eventos.BILLETERA_BLOQUEADA]
    assert resultado.bloqueo["saldoPendiente"] == UMBRAL_BLOQUEO
    aplicar_comision(repo, "pres-1", "c-3", 5000)
    assert len(repo.eventos) == 1


def test_el_pago_en_efectivo_se_registra_una_vez():
    repo = BilleterasEnMemoria()
    _, ya = registrar_pago_efectivo(repo, "c-1", 100000, date.today())
    _, otra_vez = registrar_pago_efectivo(repo, "c-1", 100000, date.today())
    assert (ya, otra_vez) == (False, True) and len(repo.pagos) == 1


# --- Ciclo de vida sobre el modelo del dominio (Contrataciones) ------------------------

def _contratacion(estado: EstadoContratacion) -> Contratacion:
    return Contratacion(id="c-1", demandanteId="d", prestadorId="p", oficioId="o",
                        fechaSolicitud=date.today(), estado=estado, valorAcordado=100000,
                        medioPago=MedioPago.EFECTIVO, porcentajeComisionAplicado=0.18, montoComision=18000)


def test_el_state_recorre_el_flujo_sobre_el_modelo_puro():
    c = _contratacion(EstadoContratacion.ACEPTADA)
    estado_de(c).iniciar(c)
    assert c.estado == EstadoContratacion.CHECK_IN and c.checkIn is not None
    estado_de(c).completar(c)
    assert c.estado == EstadoContratacion.COMPLETADA and c.checkOut is not None


@pytest.mark.parametrize("estado", [EstadoContratacion.COMPLETADA, EstadoContratacion.CANCELADA,
                                    EstadoContratacion.EN_DISPUTA])
def test_rn02_un_estado_terminal_no_admite_check_in(estado):
    with pytest.raises(TransicionInvalida, match=estado.value):
        estado_de(_contratacion(estado)).iniciar(_contratacion(estado))
