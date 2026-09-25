"""Consumidor de eventos de Comunicación: worker SQS.

Monetización publica BILLETERA_BLOQUEADA en su tema SNS cuando el saldo
pendiente de un prestador alcanza el umbral (RN-04); la cola
`comunicacion-events-queue` está suscrita a ese tema y este worker la lee y deja
la notificación al prestador. Monetización no sabe que Comunicación existe.

Garantías (las mismas que el consumidor de Monetización):

- **Idempotencia.** Cada evento se registra en `eventos_recibidos` en la misma
  transacción que la notificación; un duplicado se descarta.
- **Sin pérdida.** El mensaje se borra de la cola solo después del commit. Si
  falla (p. ej. Identidad no responde), SQS lo reentrega y, tras 5 intentos, lo
  mueve a `comunicacion-events-dlq`.

La notificación se dirige al usuario, pero el evento trae el prestador: el
`usuarioId` se le pide a Identidad, que es su dueño.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Callable

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from common import eventos
from common.db import nuevo_id
from common.observabilidad import EVENTOS_CONSUMIDOS, cabeceras_de_traza, evento_negocio, log, usar_trace_id
from common.resiliencia import Backoff, es_error_de_base, esperar_base_disponible

from .models import Notificacion
from .repositorio import RepositorioAvisos

IDENTIDAD_URL = os.getenv(
    "SERVICIO_IDENTIDAD_URL", "http://identidad.aws-local.svc.cluster.local:8001"
)

ESTADO: dict[str, Any] = {
    "activo": False,
    "conectado": False,
    "cola": eventos.QUEUE_COMUNICACION,
    "dlq": eventos.DLQ_COMUNICACION,
    "recibidos": 0,
    "procesados": 0,
    "duplicados": 0,
    "errores": 0,
    "ultimoError": None,
    "ultimoMensajeEn": None,
}
_candado = threading.Lock()

# Recibe el prestadorId y devuelve el usuarioId al que se notifica.
ResolverUsuario = Callable[[str], str]


def usuario_de_prestador(prestador_id: str) -> str:
    """usuarioId del prestador, según Identidad. Lanza si no se puede saber:
    sin destinatario no hay notificación, y el mensaje debe reintentarse."""
    respuesta = httpx.get(f"{IDENTIDAD_URL}/prestadores/{prestador_id}", timeout=5.0,
                          headers=cabeceras_de_traza())
    respuesta.raise_for_status()
    return respuesta.json()["usuarioId"]


def _contar(clave: str) -> None:
    with _candado:
        ESTADO[clave] += 1


def _texto_bloqueo(evento: dict[str, Any]) -> str:
    saldo = float(evento.get("saldoPendiente", 0))
    umbral = float(evento.get("umbralBloqueo", 0))
    return (f"Tu billetera quedó bloqueada: debes ${saldo:,.0f} COP en comisiones y el "
            f"límite es ${umbral:,.0f} COP. Liquida el saldo para volver a recibir servicios.")


def procesar_mensaje(sesiones: sessionmaker[Session], cuerpo_sqs: str, mensaje_sqs_id: str,
                     resolver_usuario: ResolverUsuario = usuario_de_prestador) -> str:
    """Procesa un mensaje y devuelve el resultado. Lanza si hay que reintentarlo."""
    evento, sns_id = eventos.desenvolver_sns(cuerpo_sqs)
    usar_trace_id(evento.get("traceId"))
    resultado = _procesar(sesiones, evento, sns_id, mensaje_sqs_id, resolver_usuario)
    EVENTOS_CONSUMIDOS.labels("comunicacion", resultado).inc()
    if resultado == "DUPLICADO":
        evento_negocio("evento_duplicado", "Aviso ya entregado: se descarta",
                       eventoId=evento.get("eventoId") or sns_id)
    elif resultado == "NOTIFICADO":
        evento_negocio("prestador_notificado", "Se avisó al prestador del bloqueo de su billetera",
                       prestadorId=evento.get("prestadorId"), billeteraId=evento.get("billeteraId"))
    return resultado


def _procesar(sesiones: sessionmaker[Session], evento: dict[str, Any], sns_id: str | None,
              mensaje_sqs_id: str, resolver_usuario: ResolverUsuario) -> str:
    evento_id = str(evento.get("eventoId") or sns_id or mensaje_sqs_id)
    tipo = str(evento.get("tipo") or evento.get("evento") or "")

    # Unidad de trabajo: la notificación y el registro del evento, juntos.
    with sesiones() as s:
        avisos = RepositorioAvisos(s)
        if avisos.ya_recibido(evento_id):
            return "DUPLICADO"

        notificacion_id = None
        if tipo == eventos.BILLETERA_BLOQUEADA and evento.get("prestadorId"):
            notificacion = Notificacion(
                id=nuevo_id(),
                usuarioId=resolver_usuario(evento["prestadorId"]),
                tipo=eventos.BILLETERA_BLOQUEADA,
                canal="PUSH",
                contenido=_texto_bloqueo(evento),
                fechaEnvio=datetime.now(),
                leida=False,
            )
            avisos.agregar_notificacion(notificacion)
            notificacion_id = notificacion.id
            resultado = "NOTIFICADO"
        else:
            # Un tipo que este contexto no atiende: se registra para no releerlo.
            resultado = "IGNORADO"

        avisos.registrar_recibido(evento_id=evento_id, tipo=tipo or "DESCONOCIDO", resultado=resultado,
                                  notificacion_id=notificacion_id, mensaje_sqs_id=mensaje_sqs_id)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return "DUPLICADO"
    return resultado


def _bucle(sesiones: sessionmaker[Session]) -> None:
    sqs = eventos.cliente("sqs")
    url = None
    backoff_sqs = Backoff(maximo=30.0)
    while True:
        base_caida = False
        try:
            if url is None:
                url = sqs.get_queue_url(QueueName=eventos.QUEUE_COMUNICACION)["QueueUrl"]
                log.info("sqs_conectado", extra={"cola": url})
            ESTADO["conectado"] = True
            backoff_sqs.reiniciar()

            respuesta = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=10)
            listos = []
            for mensaje in respuesta.get("Messages", []):
                _contar("recibidos")
                try:
                    resultado = procesar_mensaje(sesiones, mensaje["Body"], mensaje["MessageId"])
                except Exception as e:  # noqa: BLE001
                    # No se borra: SQS lo reintentará y, si insiste, irá a la DLQ.
                    _contar("errores")
                    ESTADO["ultimoError"] = f"{type(e).__name__}: {e}"
                    EVENTOS_CONSUMIDOS.labels("comunicacion", "ERROR").inc()
                    base_caida = es_error_de_base(e)
                    log.error("notificacion_fallida", extra={
                        "mensajeSqsId": mensaje["MessageId"], "error": ESTADO["ultimoError"],
                        "accion": "pausa con backoff hasta que vuelva la base" if base_caida
                                  else "SQS lo reentregará (DLQ tras 5)"})
                    if base_caida:
                        break
                    continue
                _contar("duplicados" if resultado == "DUPLICADO" else "procesados")
                ESTADO["ultimoMensajeEn"] = datetime.now()
                listos.append({"Id": mensaje["MessageId"], "ReceiptHandle": mensaje["ReceiptHandle"]})

            if listos:
                sqs.delete_message_batch(QueueUrl=url, Entries=listos)
        except Exception as e:  # noqa: BLE001 — LocalStack caído o reiniciado
            log.warning("sqs_no_disponible", extra={"endpoint": eventos.AWS_ENDPOINT_URL, "error": str(e)})
            ESTADO["conectado"] = False
            ESTADO["ultimoError"] = str(e)
            url = None
            time.sleep(backoff_sqs.siguiente())
            continue

        if base_caida:
            esperar_base_disponible(sesiones, "sqs-worker-comunicacion")


def iniciar_worker(sesiones: sessionmaker[Session]) -> bool:
    """Arranca el hilo consumidor. Devuelve si quedó activo."""
    if not eventos.activado("SQS_ENABLED"):
        log.warning("sqs_worker_deshabilitado", extra={"motivo": "SQS_ENABLED=false"})
        return False
    threading.Thread(target=_bucle, args=(sesiones,), name="sqs-worker-comunicacion", daemon=True).start()
    ESTADO["activo"] = True
    return True
