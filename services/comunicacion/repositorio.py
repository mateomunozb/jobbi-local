"""Repositorio del consumidor de eventos de Comunicación (patrón Repository).

El worker que avisa del bloqueo de una billetera trabaja con objetos del
dominio (`Notificacion`) y con su bandeja de entrada (Idempotent Receiver) sin
conocer las tablas. No hace commit: la transacción la cierra el worker, así la
notificación y el registro del evento se guardan juntos.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from .models import Notificacion
from .tablas import EventoRecibidoFila, NotificacionFila


class RepositorioAvisos:
    def __init__(self, sesion: Session) -> None:
        self._s = sesion

    def ya_recibido(self, evento_id: str) -> bool:
        return self._s.get(EventoRecibidoFila, evento_id) is not None

    def agregar_notificacion(self, notificacion: Notificacion) -> None:
        self._s.add(NotificacionFila(**notificacion.model_dump()))

    def registrar_recibido(self, *, evento_id: str, tipo: str, resultado: str,
                           notificacion_id: str | None, mensaje_sqs_id: str) -> None:
        self._s.add(EventoRecibidoFila(
            eventoId=evento_id, tipo=tipo, resultado=resultado, notificacionId=notificacion_id,
            mensajeSqsId=mensaje_sqs_id, fechaProcesado=datetime.now(),
        ))
