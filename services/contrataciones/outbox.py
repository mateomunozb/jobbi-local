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
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from common import eventos
from common.db import nuevo_id

from .tablas import EventoOutboxFila

# Tamaño máximo de PublishBatch en SNS.
LOTE = 10
INTERVALO = float(os.getenv("OUTBOX_INTERVALO_SEGUNDOS", "1.0"))

# Recibe [(eventoId, tipo, payload)] y devuelve {eventoId: mensajeSnsId} con los
# que se publicaron. Lanza una excepción si no pudo publicar ninguno.
Publicador = Callable[[list[tuple[str, str, str]]], dict[str, str]]


def registrar_evento(s: Session, tipo: str, agregado_id: str, datos: dict[str, Any]) -> EventoOutboxFila:
    """Añade el evento a la transacción en curso. No hace commit: eso es del llamador."""
    evento_id = nuevo_id()
    ahora = datetime.now()
    payload = {
        "eventoId": evento_id,
        "tipo": tipo,
        "version": 1,
        "ocurridoEn": ahora.isoformat(),
        # Claves que ya leía el consumidor original (y la prueba k6 de SNS).
        "evento": tipo,
        **datos,
    }
    fila = EventoOutboxFila(
        id=evento_id,
        tipo=tipo,
        agregadoId=agregado_id,
        payload=json.dumps(payload),
        estado="PENDIENTE",
        intentos=0,
        fechaCreacion=ahora,
    )
    s.add(fila)
    return fila


class PublicadorSns:
    """Publica en el tema SNS con PublishBatch (hasta 10 mensajes por llamada)."""

    def __init__(self) -> None:
        self._sns = eventos.cliente("sns")
        self._topic_arn: str | None = None

    def _arn(self) -> str:
        # create_topic es idempotente: si el tema existe devuelve su ARN. Evita
        # depender de que el ARN esté escrito en la configuración.
        if self._topic_arn is None:
            self._topic_arn = self._sns.create_topic(Name=eventos.TOPIC_NAME)["TopicArn"]
        return self._topic_arn

    def __call__(self, lote: list[tuple[str, str, str]]) -> dict[str, str]:
        try:
            respuesta = self._sns.publish_batch(
                TopicArn=self._arn(),
                PublishBatchRequestEntries=[
                    {
                        "Id": evento_id,
                        "Message": payload,
                        "MessageAttributes": {
                            "tipo": {"DataType": "String", "StringValue": tipo},
                        },
                    }
                    for evento_id, tipo, payload in lote
                ],
            )
        except Exception:
            # Si LocalStack se reinició, el ARN cacheado puede no existir ya.
            self._topic_arn = None
            raise
        publicados = {e["Id"]: e.get("MessageId", "") for e in respuesta.get("Successful", [])}
        if not publicados and respuesta.get("Failed"):
            raise RuntimeError(f"SNS rechazó el lote: {respuesta['Failed'][0]}")
        return publicados


class RelayOutbox:
    def __init__(self, sesiones: sessionmaker[Session], publicar: Publicador | None = None) -> None:
        self._sesiones = sesiones
        self._publicar = publicar
        self._despertar = threading.Event()
        self.activo = False
        self.ultimo_error: str | None = None
        self.ultima_vuelta: datetime | None = None

    def despertar(self) -> None:
        """Pide publicar ya, sin esperar al siguiente ciclo (se llama tras un check-out)."""
        self._despertar.set()

    def publicar_pendientes(self) -> int:
        """Una pasada: publica un lote de eventos PENDIENTE. Devuelve cuántos salieron."""
        if self._publicar is None:
            self._publicar = PublicadorSns()

        with self._sesiones() as s:
            # FOR UPDATE SKIP LOCKED: si hubiera varias réplicas del servicio,
            # cada una toma eventos distintos en vez de publicarlos dos veces.
            # (SQLite ignora la cláusula; en local solo hay un proceso.)
            filas = s.scalars(
                select(EventoOutboxFila)
                .where(EventoOutboxFila.estado == "PENDIENTE")
                .order_by(EventoOutboxFila.fechaCreacion)
                .limit(LOTE)
                .with_for_update(skip_locked=True)
            ).all()
            if not filas:
                return 0

            try:
                publicados = self._publicar([(f.id, f.tipo, f.payload) for f in filas])
                self.ultimo_error = None
            except Exception as e:  # noqa: BLE001 — cualquier fallo de red se reintenta
                publicados = {}
                self.ultimo_error = str(e)

            ahora = datetime.now()
            for fila in filas:
                if fila.id in publicados:
                    fila.estado = "PUBLICADO"
                    fila.fechaPublicacion = ahora
                    fila.mensajeSnsId = publicados[fila.id] or None
                else:
                    fila.intentos += 1
                    fila.ultimoError = self.ultimo_error or "SNS no confirmó el mensaje"
            s.commit()
            return len(publicados)

    def _bucle(self) -> None:
        print(f"[Outbox] Relay activo → SNS '{eventos.TOPIC_NAME}' en {eventos.AWS_ENDPOINT_URL}",
              flush=True)
        espera = INTERVALO
        while True:
            self._despertar.wait(timeout=espera)
            self._despertar.clear()
            try:
                # Mientras haya lotes completos se sigue publicando sin dormir,
                # que es lo que deja drenar un pico de check-outs.
                while self.publicar_pendientes() == LOTE:
                    pass
                self.ultima_vuelta = datetime.now()
                # Si SNS está caído se espera más entre intentos (hasta 10 s),
                # para no martillar un servicio que no responde.
                espera = min(espera * 2, 10.0) if self.ultimo_error else INTERVALO
                if self.ultimo_error:
                    print(f"[Outbox] SNS no disponible, se reintentará: {self.ultimo_error}",
                          flush=True)
            except Exception as e:  # noqa: BLE001 — el hilo no puede morir
                self.ultimo_error = f"Error del relay: {e}"
                print(f"[Outbox] {self.ultimo_error}", flush=True)
                time.sleep(2)

    def iniciar(self) -> bool:
        if not eventos.activado("SNS_ENABLED"):
            print("[Outbox] Relay deshabilitado por SNS_ENABLED=false: los eventos "
                  "quedarán PENDIENTE", flush=True)
            return False
        threading.Thread(target=self._bucle, name="relay-outbox", daemon=True).start()
        self.activo = True
        return True
