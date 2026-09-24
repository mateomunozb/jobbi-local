"""Consumidor del patrón Pub/Sub: worker SQS de Monetización.

Contrataciones publica CONTRATACION_COMPLETADA en el tema SNS; la cola
`monetizacion-events-queue` está suscrita a ese tema, y este worker la lee y
cobra la comisión en la billetera del prestador. Ninguno de los dos contextos
conoce al otro: si mañana otro contexto necesita el mismo evento, se suscribe
con su propia cola y el productor no cambia.

Garantías:

- **Idempotencia.** Cada evento se registra en `eventos_procesados` en la misma
  transacción que el cobro; un duplicado choca con la clave primaria y se
  descarta. SQS y el outbox entregan *al menos una vez*, así que esto no es
  opcional.
- **Sin pérdida.** El mensaje se borra de la cola solo después del commit. Si el
  procesamiento falla, SQS lo vuelve a entregar al vencer el visibility timeout
  y, tras 5 intentos, lo mueve a la DLQ (`monetizacion-events-dlq`) para
  revisarlo sin bloquear al resto.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from common import eventos
from common.enums import MedioPago

from .billetera import aplicar_comision
from .tablas import EventoProcesadoFila

HILOS = int(os.getenv("SQS_HILOS", "2"))

ESTADO: dict[str, Any] = {
    "activo": False,
    "conectado": False,
    "cola": eventos.QUEUE_NAME,
    "dlq": eventos.DLQ_NAME,
    "hilos": HILOS,
    "recibidos": 0,
    "procesados": 0,
    "duplicados": 0,
    "errores": 0,
    "porResultado": Counter(),
    "ultimoError": None,
    "ultimoMensajeEn": None,
}
_candado = threading.Lock()
_urls: dict[str, str] = {}


def _contar(clave: str, resultado: str | None = None) -> None:
    with _candado:
        ESTADO[clave] += 1
        if resultado:
            ESTADO["porResultado"][resultado] += 1


def _desenvolver(cuerpo_sqs: str) -> tuple[dict[str, Any], str | None]:
    """Devuelve (evento, MessageId de SNS). SNS envuelve el mensaje en 'Message'."""
    cuerpo = json.loads(cuerpo_sqs)
    if isinstance(cuerpo, dict) and "Message" in cuerpo and "TopicArn" in cuerpo:
        try:
            return json.loads(cuerpo["Message"]), cuerpo.get("MessageId")
        except (TypeError, ValueError):
            return {"crudo": cuerpo["Message"]}, cuerpo.get("MessageId")
    return cuerpo, None


def _fecha(valor: Any) -> datetime | None:
    """Fecha del evento en hora local sin zona, como las que guarda el servicio.

    El outbox escribe hora local; otros productores (k6, la CLI) mandan UTC con
    zona. Quitarle la zona sin convertir descuadraría la latencia en horas.
    """
    try:
        fecha = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return fecha.astimezone().replace(tzinfo=None) if fecha.tzinfo else fecha


def procesar_mensaje(sesiones: sessionmaker[Session], cuerpo_sqs: str, mensaje_sqs_id: str) -> str:
    """Procesa un mensaje y devuelve el resultado. Lanza si hay que reintentarlo."""
    evento, sns_id = _desenvolver(cuerpo_sqs)
    # El eventoId lo pone el outbox. Los mensajes publicados a mano o por k6
    # contra SNS no lo traen: para esos, el MessageId de SNS es igual de único.
    evento_id = str(evento.get("eventoId") or sns_id or mensaje_sqs_id)
    tipo = str(evento.get("tipo") or evento.get("evento") or eventos.CONTRATACION_COMPLETADA)
    contratacion_id = evento.get("contratacionId")
    prestador_id = evento.get("prestadorId")
    ocurrido = _fecha(evento.get("ocurridoEn") or evento.get("timestamp"))

    with sesiones() as s:
        if s.get(EventoProcesadoFila, evento_id) is not None:
            return "DUPLICADO"

        if tipo == eventos.CONTRATACION_COMPLETADA and contratacion_id and prestador_id:
            monto = float(evento.get("montoComision", evento.get("monto", 0.0)))
            if evento.get("medioPago", MedioPago.EFECTIVO.value) == MedioPago.EFECTIVO.value:
                _, _, ya_cobrada = aplicar_comision(s, prestador_id, contratacion_id, monto)
                resultado = "YA_COBRADA" if ya_cobrada else "COMISION_COBRADA"
            else:
                # Con pago por plataforma la comisión se retiene del pago, que
                # aún no está implementado: se registra el evento sin cobrar.
                resultado = "SIN_COBRO_MEDIO_PLATAFORMA"
        else:
            # Evento sin contratación real (p. ej. la prueba de carga que
            # publica directo en SNS): se registra para poder contarlo.
            monto = float(evento.get("monto", 0.0))
            contratacion_id = contratacion_id or evento.get("servicio_id")
            resultado = "REGISTRADO_SIN_CONTRATACION"

        ahora = datetime.now()
        s.add(EventoProcesadoFila(
            eventoId=evento_id,
            tipo=tipo,
            contratacionId=str(contratacion_id) if contratacion_id else None,
            prestadorId=prestador_id,
            monto=monto,
            resultado=resultado,
            origen="ASINCRONO_SQS",
            mensajeSqsId=mensaje_sqs_id,
            ocurridoEn=ocurrido,
            fechaProcesado=ahora,
            latenciaMs=(ahora - ocurrido).total_seconds() * 1000 if ocurrido else None,
        ))
        try:
            s.commit()
        except IntegrityError:
            # Otro hilo procesó el mismo evento a la vez y ganó: es un duplicado.
            s.rollback()
            return "DUPLICADO"
    return resultado


def _url_de(sqs, nombre: str) -> str:
    if nombre not in _urls:
        _urls[nombre] = sqs.get_queue_url(QueueName=nombre)["QueueUrl"]
    return _urls[nombre]


def estado_colas() -> dict[str, Any]:
    """Profundidad aproximada de la cola principal y de la DLQ."""
    sqs = eventos.cliente("sqs")
    colas = {}
    for clave, nombre in (("principal", eventos.QUEUE_NAME), ("dlq", eventos.DLQ_NAME)):
        try:
            atributos = sqs.get_queue_attributes(
                QueueUrl=_url_de(sqs, nombre),
                AttributeNames=["ApproximateNumberOfMessages",
                                "ApproximateNumberOfMessagesNotVisible"],
            )["Attributes"]
            colas[clave] = {
                "nombre": nombre,
                "visibles": int(atributos.get("ApproximateNumberOfMessages", 0)),
                "enVuelo": int(atributos.get("ApproximateNumberOfMessagesNotVisible", 0)),
            }
        except Exception as e:  # noqa: BLE001
            _urls.pop(nombre, None)
            colas[clave] = {"nombre": nombre, "error": str(e)}
    return colas


def _bucle(sesiones: sessionmaker[Session]) -> None:
    sqs = eventos.cliente("sqs")
    while True:
        try:
            url = _url_de(sqs, eventos.QUEUE_NAME)
            if not ESTADO["conectado"]:
                print(f"[SQS Worker] Conectado a {url}", flush=True)
            ESTADO["conectado"] = True

            # Long polling: la llamada espera hasta 10 s a que haya mensajes, en
            # vez de preguntar en vacío varias veces por segundo.
            respuesta = sqs.receive_message(
                QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=10,
            )
            listos = []
            for mensaje in respuesta.get("Messages", []):
                _contar("recibidos")
                try:
                    resultado = procesar_mensaje(sesiones, mensaje["Body"], mensaje["MessageId"])
                except Exception as e:  # noqa: BLE001
                    # No se borra: SQS lo reintentará y, si insiste, irá a la DLQ.
                    _contar("errores")
                    ESTADO["ultimoError"] = f"{type(e).__name__}: {e}"
                    print(f"[SQS Worker] Error, el mensaje se reintentará: {e}", flush=True)
                    continue
                _contar("duplicados" if resultado == "DUPLICADO" else "procesados", resultado)
                ESTADO["ultimoMensajeEn"] = datetime.now()
                listos.append({"Id": mensaje["MessageId"], "ReceiptHandle": mensaje["ReceiptHandle"]})

            if listos:
                sqs.delete_message_batch(QueueUrl=url, Entries=listos)
        except Exception as e:  # noqa: BLE001 — LocalStack caído o reiniciado
            if ESTADO["conectado"]:
                print(f"[SQS Worker] Se perdió la conexión con SQS: {e}", flush=True)
            else:
                print(f"[SQS Worker] Esperando la cola en {eventos.AWS_ENDPOINT_URL}: {e}", flush=True)
            ESTADO["conectado"] = False
            ESTADO["ultimoError"] = str(e)
            _urls.clear()
            time.sleep(3)


def iniciar_worker(sesiones: sessionmaker[Session]) -> bool:
    """Arranca los hilos consumidores. Devuelve si quedaron activos."""
    if not eventos.activado("SQS_ENABLED"):
        print("[SQS Worker] Deshabilitado por SQS_ENABLED=false", flush=True)
        return False
    for i in range(HILOS):
        threading.Thread(target=_bucle, args=(sesiones,), name=f"sqs-worker-{i}", daemon=True).start()
    ESTADO["activo"] = True
    return True
