"""Repositorios de Monetización (patrón Repository).

Son lo único del camino del cobro que conoce SQLAlchemy y las tablas: reciben y
devuelven objetos del dominio (`BilleteraPrestador`, `MovimientoBilletera`,
`Pago`), nunca filas del ORM. La regla del cobro (`billetera.py`) queda así
agnóstica al motor: se prueba con un doble en memoria y no sabe si por debajo
hay PostgreSQL o SQLite.

No hacen commit. La transacción (Unit of Work) la abre y la cierra la capa de
aplicación —el consumidor SQS—, de modo que cargo, pago, evento del outbox y
registro en la bandeja de entrada se guardan juntos o no se guarda ninguno.

Las consultas de solo lectura de `main.py` (listados, resúmenes, métricas) no
pasan por aquí: leen directo, como lado de consulta separado del de escritura.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from common.db import nuevo_id
from common.outbox import registrar_evento

from .models import BilleteraPrestador, MovimientoBilletera, Pago
from .tablas import (
    BilleteraPrestadorFila,
    EventoOutboxMonetizacionFila,
    EventoProcesadoFila,
    MovimientoBilleteraFila,
    PagoFila,
)

CARGO_COMISION = "COMISION"


class RepositorioBilleteras:
    """Agregado BilleteraPrestador: la billetera, sus movimientos y los pagos en efectivo."""

    def __init__(self, sesion: Session) -> None:
        self._s = sesion

    def _fila(self, prestador_id: str) -> BilleteraPrestadorFila | None:
        return self._s.scalars(select(BilleteraPrestadorFila).where(
            BilleteraPrestadorFila.prestadorId == prestador_id)).first()

    def obtener_o_crear(self, prestador_id: str) -> BilleteraPrestador:
        """La billetera del prestador; se abre con su primer movimiento, no en el registro."""
        fila = self._fila(prestador_id)
        if fila is None:
            fila = BilleteraPrestadorFila(id=nuevo_id(), prestadorId=prestador_id,
                                          saldoPendiente=0.0, bloqueada=False)
            self._s.add(fila)
            self._s.flush()
        return BilleteraPrestador.model_validate(fila)

    def cargo_de(self, billetera_id: str, contratacion_id: str) -> MovimientoBilletera | None:
        """El cargo de comisión ya aplicado por esa contratación, si existe."""
        fila = self._s.scalars(select(MovimientoBilleteraFila).where(
            MovimientoBilleteraFila.billeteraId == billetera_id,
            MovimientoBilleteraFila.contratacionId == contratacion_id,
            MovimientoBilleteraFila.tipo == CARGO_COMISION,
        )).first()
        return MovimientoBilletera.model_validate(fila) if fila else None

    def registrar_cargo(self, billetera_id: str, contratacion_id: str,
                        monto: float) -> tuple[MovimientoBilletera, BilleteraPrestador]:
        """Registra el cargo y suma `monto` al saldo. Devuelve el movimiento y la billetera ya actualizada.

        El saldo se suma en SQL (saldo = saldo + monto) y no leyendo y
        reescribiendo: con varios consumidores en paralelo, la segunda forma
        pierde cobros. La billetera se relee después del UPDATE, así que refleja
        lo que otro consumidor haya confirmado mientras tanto.
        """
        movimiento = MovimientoBilleteraFila(
            id=nuevo_id(), billeteraId=billetera_id, contratacionId=contratacion_id,
            tipo=CARGO_COMISION,
            # Negativo: es lo que el prestador le debe a la plataforma.
            monto=-monto, fecha=date.today(),
        )
        self._s.add(movimiento)
        self._s.execute(
            update(BilleteraPrestadorFila)
            .where(BilleteraPrestadorFila.id == billetera_id)
            .values(saldoPendiente=BilleteraPrestadorFila.saldoPendiente + monto)
        )
        fila = self._s.get(BilleteraPrestadorFila, billetera_id)
        self._s.refresh(fila)
        return MovimientoBilletera.model_validate(movimiento), BilleteraPrestador.model_validate(fila)

    def fijar_bloqueo(self, billetera_id: str, bloqueada: bool) -> None:
        self._s.get(BilleteraPrestadorFila, billetera_id).bloqueada = bloqueada

    def pago_de(self, contratacion_id: str) -> Pago | None:
        fila = self._s.scalars(select(PagoFila).where(PagoFila.contratacionId == contratacion_id)).first()
        return Pago.model_validate(fila) if fila else None

    def agregar_pago(self, pago: Pago) -> None:
        self._s.add(PagoFila(**pago.model_dump()))

    def registrar_evento(self, tipo: str, agregado_id: str, datos: dict[str, Any]) -> None:
        """Deja el evento en el outbox de Monetización, en la misma transacción."""
        registrar_evento(self._s, EventoOutboxMonetizacionFila, tipo, agregado_id, datos)


class BandejaDeEntrada:
    """Eventos ya procesados por el consumidor (Idempotent Receiver)."""

    def __init__(self, sesion: Session) -> None:
        self._s = sesion

    def ya_procesado(self, evento_id: str) -> bool:
        return self._s.get(EventoProcesadoFila, evento_id) is not None

    def registrar(self, *, evento_id: str, tipo: str, contratacion_id: str | None, prestador_id: str | None,
                  monto: float, resultado: str, mensaje_sqs_id: str, ocurrido: datetime | None,
                  procesado: datetime) -> None:
        self._s.add(EventoProcesadoFila(
            eventoId=evento_id, tipo=tipo, contratacionId=contratacion_id, prestadorId=prestador_id,
            monto=monto, resultado=resultado, origen="ASINCRONO_SQS", mensajeSqsId=mensaje_sqs_id,
            ocurridoEn=ocurrido, fechaProcesado=procesado,
            latenciaMs=(procesado - ocurrido).total_seconds() * 1000 if ocurrido else None,
        ))
