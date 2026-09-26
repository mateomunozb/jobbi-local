"""Mensajería entre contextos: contrato de los eventos y clientes AWS.

El productor (Contrataciones) y el consumidor (Monetización) no se conocen: solo
comparten el nombre del evento y la forma de su carga, que se definen aquí. La
infraestructura es SNS (el tema al que se publica) y SQS (la cola de la que lee
cada suscriptor), emulada con LocalStack.

Lo que es configuración —endpoint, región, nombres— sale de variables de entorno,
de modo que la misma imagen apunta a LocalStack dentro del clúster, a
`localhost:4566` en local o a AWS real sin cambiar el código.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
from botocore.config import Config

CONTRATACION_COMPLETADA = "CONTRATACION_COMPLETADA"
# Monetización → Comunicación: la billetera de un prestador acaba de cruzar el
# umbral de saldo pendiente (RN-04) y hay que avisarle.
BILLETERA_BLOQUEADA = "BILLETERA_BLOQUEADA"

AWS_ENDPOINT_URL = os.getenv(
    "AWS_ENDPOINT_URL", "http://localstack.aws-local.svc.cluster.local:4566"
)
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

TOPIC_NAME = os.getenv("SNS_TOPIC_NAME", "contratacion-completada-topic")
QUEUE_NAME = os.getenv("QUEUE_NAME", "monetizacion-events-queue")
DLQ_NAME = os.getenv("QUEUE_DLQ_NAME", "monetizacion-events-dlq")

TOPIC_BILLETERA_BLOQUEADA = os.getenv("SNS_TOPIC_BILLETERA_BLOQUEADA", "billetera-bloqueada-topic")
QUEUE_COMUNICACION = os.getenv("QUEUE_COMUNICACION", "comunicacion-events-queue")
DLQ_COMUNICACION = os.getenv("QUEUE_COMUNICACION_DLQ", "comunicacion-events-dlq")


def activado(variable: str, por_defecto: str = "true") -> bool:
    return os.getenv(variable, por_defecto).lower() in ("1", "true", "yes")


def desenvolver_sns(cuerpo_sqs: str) -> tuple[dict[str, Any], str | None]:
    """Devuelve (evento, MessageId de SNS). SNS envuelve el mensaje en 'Message'."""
    cuerpo = json.loads(cuerpo_sqs)
    if isinstance(cuerpo, dict) and "Message" in cuerpo and "TopicArn" in cuerpo:
        try:
            return json.loads(cuerpo["Message"]), cuerpo.get("MessageId")
        except (TypeError, ValueError):
            return {"crudo": cuerpo["Message"]}, cuerpo.get("MessageId")
    return cuerpo, None


def cliente(servicio: str):
    """Cliente boto3 contra el endpoint configurado.

    Los timeouts son cortos a propósito: si LocalStack no responde, el relay y
    el worker deben enterarse rápido y reintentar, no quedarse colgados.
    """
    return boto3.client(
        servicio,
        endpoint_url=AWS_ENDPOINT_URL,
        region_name=AWS_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
        config=Config(connect_timeout=3, read_timeout=20, retries={"max_attempts": 2}),
    )
