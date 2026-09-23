"""Worker asíncrono que consume el evento CONTRATACION_COMPLETADA desde SQS.

Es la mitad consumidora del patrón Pub/Sub (SNS → SQS) que ya validaba el
simulador original: Contrataciones publica en el tema y Monetización registra
el cobro sin acoplarse al productor. Se conserva intacto para que las pruebas
de carga con k6 sigan siendo válidas.
"""

from __future__ import annotations

import json
import os
import threading
import time

import boto3

AWS_ENDPOINT_URL = os.getenv(
    "AWS_ENDPOINT_URL", "http://localstack.aws-local.svc.cluster.local:4566"
)
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
QUEUE_NAME = os.getenv("QUEUE_NAME", "monetizacion-events-queue")
# Fuera de Kubernetes no hay LocalStack: el worker se apaga y el resto de la API
# sigue respondiendo con normalidad.
SQS_ENABLED = os.getenv("SQS_ENABLED", "true").lower() in ("1", "true", "yes")

COBROS_REGISTRADOS: list[dict] = []

_sqs = boto3.client(
    "sqs",
    endpoint_url=AWS_ENDPOINT_URL,
    region_name=AWS_REGION,
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
)


def _poll_sqs_messages() -> None:
    queue_url = None

    while not queue_url:
        try:
            queue_url = _sqs.get_queue_url(QueueName=QUEUE_NAME).get("QueueUrl")
            print(f"[SQS Worker] Conectado a SQS. QueueUrl: {queue_url}", flush=True)
        except Exception as e:
            print(f"[SQS Worker] Esperando LocalStack en {AWS_ENDPOINT_URL}: {e}", flush=True)
            time.sleep(3)

    while True:
        try:
            respuesta = _sqs.receive_message(
                QueueUrl=queue_url, MaxNumberOfMessages=5, WaitTimeSeconds=2
            )
            for msg in respuesta.get("Messages", []):
                body = json.loads(msg["Body"])

                # Si el mensaje llegó enrutado por SNS viene envuelto en "Message".
                if "Message" in body:
                    try:
                        body = json.loads(body["Message"])
                    except Exception:
                        pass

                registro = {
                    "id": f"COBRO-EVENTO-{len(COBROS_REGISTRADOS) + 1}",
                    "evento": body.get("evento", "CONTRATACION_COMPLETADA"),
                    "monto": float(body.get("monto", 0.0)),
                    "servicio_id": str(body.get("servicio_id", "N/A")),
                    "tipo": "ASINCRONO_SQS",
                }
                COBROS_REGISTRADOS.append(registro)
                print(f"[SQS Worker] Cobro procesado: {registro}", flush=True)

                _sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
        except Exception as e:
            print(f"[SQS Worker] Excepción: {e}", flush=True)
            time.sleep(2)


def iniciar_worker() -> bool:
    """Arranca el polling en un hilo demonio. Devuelve si quedó activo."""
    if not SQS_ENABLED:
        print("[SQS Worker] Deshabilitado por SQS_ENABLED=false", flush=True)
        return False
    threading.Thread(target=_poll_sqs_messages, daemon=True).start()
    return True
