import os
import json
import time
import threading
from fastapi import FastAPI
import boto3

app = FastAPI(title="Servicio de Monetización - JOBBI Simulator")

AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL", "http://localstack.aws-local.svc.cluster.local:4566")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
QUEUE_NAME = "monetizacion-events-queue"

COBROS_REGISTRADOS = []

# Cliente SQS
sqs = boto3.client(
    'sqs',
    endpoint_url=AWS_ENDPOINT_URL,
    region_name=AWS_REGION,
    aws_access_key_id="test",
    aws_secret_access_key="test"
)

@app.get("/health")
def health_check():
    return {"status": "UP", "service": "monetizacion"}

@app.get("/cobros")
def listar_cobros():
    return {"total": len(COBROS_REGISTRADOS), "cobros": COBROS_REGISTRADOS}

@app.post("/cobrar")
def procesar_cobro_sincrono(servicio_id: str, monto: float, usuario_id: str):
    registro = {
        "id": f"COBRO-{len(COBROS_REGISTRADOS) + 1}",
        "servicio_id": servicio_id,
        "monto": monto,
        "usuario_id": usuario_id,
        "tipo": "SINCRONO"
    }
    COBROS_REGISTRADOS.append(registro)
    return {"mensaje": "Cobro procesado exitosamente", "detalle": registro}

def poll_sqs_messages():
    """Worker en segundo plano para escuchar eventos de SQS"""
    queue_url = None
    
    # 1. Obtener la URL dinámica de la cola desde SQS
    while not queue_url:
        try:
            res = sqs.get_queue_url(QueueName=QUEUE_NAME)
            queue_url = res.get('QueueUrl')
            print(f"[SQS Worker] Conectado exitosamente a SQS. QueueUrl: {queue_url}", flush=True)
        except Exception as e:
            print(f"[SQS Worker Error] Esperando a SQS / LocalStack en {AWS_ENDPOINT_URL}: {e}", flush=True)
            time.sleep(3)

    # 2. Polling loop
    while True:
        try:
            response = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=5,
                WaitTimeSeconds=2
            )
            messages = response.get('Messages', [])
            for msg in messages:
                print(f"[SQS Worker] Mensaje recibido de SQS: {msg['Body']}", flush=True)
                body = json.loads(msg['Body'])
                
                # Desenvolver el mensaje si viene enrutado por SNS
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
                    "tipo": "ASINCRONO_SQS"
                }
                COBROS_REGISTRADOS.append(registro)
                print(f"[SQS Worker] Cobro procesado e insertado: {registro}", flush=True)

                # Eliminar de la cola
                sqs.delete_message(
                    QueueUrl=queue_url,
                    ReceiptHandle=msg['ReceiptHandle']
                )
        except Exception as e:
            print(f"[SQS Worker Exception]: {e}", flush=True)
            time.sleep(2)

# Iniciar worker en hilo secundario
threading.Thread(target=poll_sqs_messages, daemon=True).start()