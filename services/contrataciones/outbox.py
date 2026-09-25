"""Productor del patrón Pub/Sub: Transactional Outbox + relay hacia SNS.

Publicar directamente en SNS desde el endpoint de check-out tendría dos fallos
posibles y ninguno bueno: si SNS falla después del commit, la contratación queda
completada sin que Monetización se entere (comisión perdida); si se publica
antes del commit y el commit falla, se cobra un servicio que no se cerró.

El outbox separa las dos cosas:

1. `registrar_evento` escribe el evento en la tabla `outbox_eventos` dentro de la
   misma transacción que el check-out. O se guardan los dos, o ninguno.
2. `RelayOutbox` (un hilo en segundo plano) lee los eventos PENDIENTE, los
   publica en SNS en lotes y los marca PUBLICADO. Si SNS no responde, el evento
   sigue PENDIENTE y se reintenta en la siguiente vuelta.

La garantía resultante es *al menos una vez*: un evento puede publicarse dos
veces (p. ej. si el Pod muere entre el publish y el commit), y por eso el
consumidor descarta duplicados por `eventoId`.

El mecanismo vive en `common/outbox.py` porque Monetización también produce
eventos (BILLETERA_BLOQUEADA); aquí solo se ata a la tabla y al tema de este
contexto.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from common import eventos
from common import outbox as _outbox
from common.outbox import INTERVALO, LOTE, Publicador  # noqa: F401 — API de este módulo

from .tablas import EventoOutboxFila


def registrar_evento(s: Session, tipo: str, agregado_id: str, datos: dict[str, Any]) -> EventoOutboxFila:
    """Añade el evento a la transacción en curso. No hace commit: eso es del llamador."""
    return _outbox.registrar_evento(s, EventoOutboxFila, tipo, agregado_id, datos)


class RelayOutbox(_outbox.RelayOutbox):
    def __init__(self, sesiones: sessionmaker[Session], publicar: Publicador | None = None) -> None:
        super().__init__(sesiones, EventoOutboxFila, eventos.TOPIC_NAME, publicar)
