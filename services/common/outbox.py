"""Transactional Outbox + Polling Publisher, compartido por los productores.

Publicar directamente en SNS desde el endpoint que cambia el estado tendría dos
fallos posibles y ninguno bueno: si SNS falla después del commit, el hecho de
negocio queda guardado sin que nadie se entere; si se publica antes del commit
y el commit falla, se anuncia algo que no ocurrió.

El outbox separa las dos cosas:

1. `registrar_evento` escribe el evento en la tabla outbox del contexto dentro
   de la misma transacción que el cambio de negocio. O se guardan los dos, o
   ninguno.
2. `RelayOutbox` (un hilo en segundo plano) lee los eventos PENDIENTE, los
   publica en SNS en lotes y los marca PUBLICADO. Si SNS no responde, el evento
   sigue PENDIENTE y se reintenta en la siguiente vuelta.

La garantía resultante es *al menos una vez*: un evento puede publicarse dos
veces (p. ej. si el Pod muere entre el publish y el commit), y por eso cada
consumidor descarta duplicados por `eventoId`.

Cada contexto productor es dueño de su propia tabla outbox (en su propia base);
lo único que comparten es este mecanismo. La tabla solo necesita las columnas
`id, tipo, agregadoId, payload, estado, intentos, ultimoError, fechaCreacion,
fechaPublicacion, mensajeSnsId`.
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

from . import eventos
from .db import nuevo_id
from .observabilidad import EVENTOS_PUBLICADOS, log, trace_id
from .resiliencia import es_error_de_base, esperar_base_disponible

# Tamaño máximo de PublishBatch en SNS.
LOTE = 10
INTERVALO = float(os.getenv("OUTBOX_INTERVALO_SEGUNDOS", "1.0"))

# Recibe [(eventoId, tipo, payload)] y devuelve {eventoId: mensajeSnsId} con los
# que se publicaron. Lanza una excepción si no pudo publicar ninguno.
Publicador = Callable[[list[tuple[str, str, str]]], dict[str, str]]


def registrar_evento(s: Session, fila_outbox: type, tipo: str, agregado_id: str,
                     datos: dict[str, Any]) -> Any:
    """Añade el evento a la transacción en curso. No hace commit: eso es del llamador."""
    evento_id = nuevo_id()
    ahora = datetime.now()
    payload = {
        "eventoId": evento_id,
        "tipo": tipo,
        "version": 1,
        "ocurridoEn": ahora.isoformat(),
        # Correlación de punta a punta: el consumidor retoma este mismo id.
        "traceId": trace_id() or nuevo_id().replace("-", ""),
        # Claves que ya leía el consumidor original (y la prueba k6 de SNS).
        "evento": tipo,
        **datos,
    }
    fila = fila_outbox(
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
    """Publica en un tema SNS con PublishBatch (hasta 10 mensajes por llamada)."""

    def __init__(self, tema: str) -> None:
        self._tema = tema
        self._sns = eventos.cliente("sns")
        self._topic_arn: str | None = None

    def _arn(self) -> str:
        # create_topic es idempotente: si el tema existe devuelve su ARN. Evita
        # depender de que el ARN esté escrito en la configuración.
        if self._topic_arn is None:
            self._topic_arn = self._sns.create_topic(Name=self._tema)["TopicArn"]
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
    """Polling Publisher: sondea la tabla outbox y publica lo PENDIENTE en `tema`."""

    def __init__(self, sesiones: sessionmaker[Session], fila_outbox: type, tema: str,
                 publicar: Publicador | None = None) -> None:
        self._sesiones = sesiones
        self._fila = fila_outbox
        self.tema = tema
        self._publicar = publicar
        self._despertar = threading.Event()
        self.activo = False
        self.ultimo_error: str | None = None
        self.ultima_vuelta: datetime | None = None

    def despertar(self) -> None:
        """Pide publicar ya, sin esperar al siguiente ciclo."""
        self._despertar.set()

    def publicar_pendientes(self) -> int:
        """Una pasada: publica un lote de eventos PENDIENTE. Devuelve cuántos salieron."""
        if self._publicar is None:
            self._publicar = PublicadorSns(self.tema)

        fila_outbox = self._fila
        with self._sesiones() as s:
            # FOR UPDATE SKIP LOCKED: si hubiera varias réplicas del servicio,
            # cada una toma eventos distintos en vez de publicarlos dos veces.
            # (SQLite ignora la cláusula; en local solo hay un proceso.)
            filas = s.scalars(
                select(fila_outbox)
                .where(fila_outbox.estado == "PENDIENTE")
                .order_by(fila_outbox.fechaCreacion)
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

            if publicados:
                log.info("eventos_publicados", extra={"tema": self.tema, "cantidad": len(publicados)})
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
            EVENTOS_PUBLICADOS.labels(self.tema).inc(len(publicados))
            return len(publicados)

    def _bucle(self) -> None:
        log.info("relay_activo", extra={"tema": self.tema, "endpoint": eventos.AWS_ENDPOINT_URL})
        espera = INTERVALO
        while True:
            self._despertar.wait(timeout=espera)
            self._despertar.clear()
            try:
                # Mientras haya lotes completos se sigue publicando sin dormir,
                # que es lo que deja drenar un pico de eventos.
                while self.publicar_pendientes() == LOTE:
                    pass
                self.ultima_vuelta = datetime.now()
                # Si SNS está caído se espera más entre intentos (hasta 10 s),
                # para no martillar un servicio que no responde.
                espera = min(espera * 2, 10.0) if self.ultimo_error else INTERVALO
                if self.ultimo_error:
                    log.warning("sns_no_disponible", extra={
                        "tema": self.tema, "error": self.ultimo_error, "reintentoEnSegundos": espera})
            except Exception as e:  # noqa: BLE001 — el hilo no puede morir
                self.ultimo_error = f"Error del relay: {e}"
                log.error("relay_error", extra={"tema": self.tema, "error": self.ultimo_error})
                if es_error_de_base(e):
                    # Base caída: los eventos siguen a salvo en el outbox; se
                    # espera con backoff exponencial y se retoma donde quedó.
                    esperar_base_disponible(self._sesiones, f"relay-{self.tema}")
                else:
                    time.sleep(2)

    def iniciar(self) -> bool:
        if not eventos.activado("SNS_ENABLED"):
            log.warning("relay_deshabilitado", extra={
                "tema": self.tema, "motivo": "SNS_ENABLED=false: los eventos quedarán PENDIENTE"})
            return False
        threading.Thread(target=self._bucle, name=f"relay-outbox-{self.tema}", daemon=True).start()
        self.activo = True
        return True
