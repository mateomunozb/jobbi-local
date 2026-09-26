"""Regla de cobro de comisiones sobre la billetera del prestador.

La usa un solo camino: el worker SQS, disparado por el evento
CONTRATACION_COMPLETADA (ADR-002). Es idempotente por contratación: aunque el
evento llegue dos veces, la comisión se carga una sola vez.

No conoce la base de datos: trabaja con objetos del dominio a través de un
repositorio (`repositorio.py`). Por eso se prueba con un doble en memoria.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from common import eventos
from common.db import nuevo_id

from .especificaciones import UMBRAL_BLOQUEO, EspecificacionPrestadorBloqueado  # noqa: F401
from .models import BilleteraPrestador, MovimientoBilletera, Pago

BLOQUEO = EspecificacionPrestadorBloqueado()


class Billeteras(Protocol):
    """Lo que la regla necesita del almacén (lo implementa RepositorioBilleteras)."""

    def obtener_o_crear(self, prestador_id: str) -> BilleteraPrestador: ...
    def cargo_de(self, billetera_id: str, contratacion_id: str) -> MovimientoBilletera | None: ...
    def registrar_cargo(self, billetera_id: str, contratacion_id: str,
                        monto: float) -> tuple[MovimientoBilletera, BilleteraPrestador]: ...
    def fijar_bloqueo(self, billetera_id: str, bloqueada: bool) -> None: ...
    def pago_de(self, contratacion_id: str) -> Pago | None: ...
    def agregar_pago(self, pago: Pago) -> None: ...
    def registrar_evento(self, tipo: str, agregado_id: str, datos: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class ResultadoCobro:
    billetera: BilleteraPrestador
    movimiento: MovimientoBilletera
    yaCobrada: bool
    # Datos del BILLETERA_BLOQUEADA emitido, si este cargo cruzó el umbral.
    bloqueo: dict[str, Any] | None = None


def aplicar_comision(billeteras: Billeteras, prestador_id: str, contratacion_id: str,
                     monto: float) -> ResultadoCobro:
    """Carga la comisión en la billetera del prestador. No hace commit.

    Idempotente por contratación: si ya existe el cargo de esa contratación, lo
    devuelve sin volver a cobrar.
    """
    billetera = billeteras.obtener_o_crear(prestador_id)
    existente = billeteras.cargo_de(billetera.id, contratacion_id)
    if existente:
        return ResultadoCobro(billetera, existente, yaCobrada=True)

    movimiento, billetera = billeteras.registrar_cargo(billetera.id, contratacion_id, abs(monto))
    # El estado anterior se lee después del cargo: si otro consumidor bloqueó
    # esta billetera en paralelo, su commit ya es visible y el aviso no se
    # emite dos veces.
    estaba_bloqueada = billetera.bloqueada
    bloqueada = BLOQUEO.es_satisfecha_por(billetera)
    if bloqueada != estaba_bloqueada:
        billeteras.fijar_bloqueo(billetera.id, bloqueada)
        billetera = billetera.model_copy(update={"bloqueada": bloqueada})

    bloqueo = None
    if bloqueada and not estaba_bloqueada:
        bloqueo = {
            "billeteraId": billetera.id,
            "prestadorId": prestador_id,
            "contratacionId": contratacion_id,
            "saldoPendiente": billetera.saldoPendiente,
            "umbralBloqueo": BLOQUEO.umbral,
        }
        # Transactional Outbox: el aviso se guarda con el cargo que lo causó.
        billeteras.registrar_evento(eventos.BILLETERA_BLOQUEADA, billetera.id, bloqueo)
    return ResultadoCobro(billetera, movimiento, yaCobrada=False, bloqueo=bloqueo)


def registrar_pago_efectivo(billeteras: Billeteras, contratacion_id: str, valor: float,
                            fecha: date) -> tuple[Pago, bool]:
    """Deja constancia del pago en efectivo de una contratación. No hace commit.

    El dinero pasó de mano en mano sin tocar la plataforma: no hay pasarela que
    lo confirme, lo confirma el check-out. Idempotente por contratación; devuelve
    (pago, yaRegistrado).
    """
    existente = billeteras.pago_de(contratacion_id)
    if existente:
        return existente, True
    pago = Pago(id=nuevo_id(), contratacionId=contratacion_id, monto=valor, estado="APROBADO",
                fechaPago=fecha, referenciaPasarela="EFECTIVO")
    billeteras.agregar_pago(pago)
    return pago, False
