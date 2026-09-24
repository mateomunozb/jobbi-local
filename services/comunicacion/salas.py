"""Salas de chat en tiempo real (WebSocket).

Cada conversación abierta en algún navegador es una sala: el conjunto de
WebSockets conectados a ella. Cuando alguien escribe, el mensaje se guarda en la
base y se difunde a toda la sala, incluido quien lo envió (le sirve de
confirmación).

Las salas viven en la memoria de este proceso, así que el chat en tiempo real
supone una sola réplica de Comunicación. Con varias, cada una solo conocería a
sus propios clientes y haría falta un canal compartido entre réplicas (p. ej.
Redis pub/sub o un tema SNS con una cola por réplica).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class Salas:
    # Sin candado: todo esto corre en el event loop y ninguna operación sobre
    # los conjuntos cede el control a mitad de camino.
    def __init__(self) -> None:
        self._salas: dict[str, set[WebSocket]] = defaultdict(set)

    def unir(self, conversacion_id: str, ws: WebSocket) -> int:
        self._salas[conversacion_id].add(ws)
        return len(self._salas[conversacion_id])

    def salir(self, conversacion_id: str, ws: WebSocket) -> int:
        sala = self._salas.get(conversacion_id)
        if sala is None:
            return 0
        sala.discard(ws)
        if not sala:
            del self._salas[conversacion_id]
            return 0
        return len(sala)

    def conectados(self, conversacion_id: str) -> int:
        return len(self._salas.get(conversacion_id, ()))

    async def difundir(self, conversacion_id: str, evento: dict[str, Any],
                       excepto: WebSocket | None = None) -> None:
        """Envía el evento a toda la sala. Un cliente caído no frena a los demás."""
        destinos = [ws for ws in list(self._salas.get(conversacion_id, ())) if ws is not excepto]
        resultados = await asyncio.gather(
            *(ws.send_json(evento) for ws in destinos), return_exceptions=True,
        )
        for ws, resultado in zip(destinos, resultados):
            if isinstance(resultado, Exception):
                self.salir(conversacion_id, ws)


salas = Salas()
