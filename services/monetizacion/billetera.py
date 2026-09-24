"""Regla de cobro de comisiones sobre la billetera del prestador.

La usan dos caminos: el worker SQS (el cobro normal, disparado por el evento
CONTRATACION_COMPLETADA) y el endpoint `POST /comisiones` (cobro síncrono, que
el gateway usa solo cuando corre sin Pub/Sub). Vivir en un solo lugar garantiza
que ambos cobran igual y que ninguno cobra dos veces la misma contratación.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from common.db import nuevo_id

from .tablas import BilleteraPrestadorFila, MovimientoBilleteraFila

# Por encima de este saldo la billetera se bloquea: la comisión en efectivo la
# cobra el prestador de su cliente y queda debiéndosela a la plataforma, así que
# acumular deuda sin liquidar no puede ser gratis.
UMBRAL_BLOQUEO = 150000.0


def billetera_de(s: Session, prestador_id: str) -> BilleteraPrestadorFila:
    """Devuelve la billetera del prestador, creándola en su primer movimiento.

    No se abre en el registro: un prestador que nunca ha trabajado no tiene
    nada que liquidar, y la base no guarda filas que no representen un hecho.
    """
    billetera = s.scalars(select(BilleteraPrestadorFila).where(
        BilleteraPrestadorFila.prestadorId == prestador_id
    )).first()
    if billetera is None:
        billetera = BilleteraPrestadorFila(
            id=nuevo_id(), prestadorId=prestador_id, saldoPendiente=0.0, bloqueada=False,
        )
        s.add(billetera)
        s.flush()
    return billetera


def aplicar_comision(
    s: Session, prestador_id: str, contratacion_id: str, monto: float,
) -> tuple[BilleteraPrestadorFila, MovimientoBilleteraFila, bool]:
    """Carga la comisión y devuelve (billetera, movimiento, yaCobrada). No hace commit.

    Idempotente por contratación: si ya existe el movimiento COMISION de esa
    contratación, lo devuelve sin volver a cobrar.
    """
    billetera = billetera_de(s, prestador_id)

    existente = s.scalars(select(MovimientoBilleteraFila).where(
        MovimientoBilleteraFila.billeteraId == billetera.id,
        MovimientoBilleteraFila.contratacionId == contratacion_id,
        MovimientoBilleteraFila.tipo == "COMISION",
    )).first()
    if existente:
        return billetera, existente, True

    monto = abs(monto)
    movimiento = MovimientoBilleteraFila(
        id=nuevo_id(),
        billeteraId=billetera.id,
        contratacionId=contratacion_id,
        tipo="COMISION",
        # Negativo: es lo que el prestador le debe a la plataforma.
        monto=-monto,
        fecha=date.today(),
    )
    s.add(movimiento)

    # El saldo se suma en SQL (saldo = saldo + monto) y no leyendo y
    # reescribiendo en Python: con varios consumidores en paralelo, la segunda
    # forma pierde cobros cuando dos escriben la misma billetera a la vez.
    s.execute(
        update(BilleteraPrestadorFila)
        .where(BilleteraPrestadorFila.id == billetera.id)
        .values(saldoPendiente=BilleteraPrestadorFila.saldoPendiente + monto)
    )
    s.refresh(billetera)
    billetera.bloqueada = billetera.saldoPendiente > UMBRAL_BLOQUEO
    return billetera, movimiento, False
