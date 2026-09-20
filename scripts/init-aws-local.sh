#!/usr/bin/env bash

ENDPOINT_URL="http://localhost:4566"
REGION="us-east-1"

echo "=== Aprovisionando infraestructura AWS Local (LocalStack) ==="

# 1. Crear el Tema SNS (Evento: ContratacionCompletada)
echo "1. Creando SNS Topic: contratacion-completada-topic..."
aws --endpoint-url=$ENDPOINT_URL sns create-topic \
    --name contratacion-completada-topic \
    --region $REGION

# 2. Crear la Cola SQS para el servicio de Monetización
echo "2. Creando SQS Queue: monetizacion-events-queue..."
aws --endpoint-url=$ENDPOINT_URL sqs create-queue \
    --queue-name monetizacion-events-queue \
    --region $REGION

# 3. Suscribir la Cola SQS al Tema SNS (Pub/Sub)
echo "3. Suscribiendo SQS al SNS Topic..."
TOPIC_ARN="arn:aws:sns:us-east-1:000000000000:contratacion-completada-topic"
QUEUE_ARN="arn:aws:sqs:us-east-1:000000000000:monetizacion-events-queue"

aws --endpoint-url=$ENDPOINT_URL sns subscribe \
    --topic-arn $TOPIC_ARN \
    --protocol sqs \
    --notification-endpoint $QUEUE_ARN \
    --region $REGION

echo "=== Aprovisionamiento completado con éxito ==="