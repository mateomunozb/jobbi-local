#!/usr/bin/env bash
# Aprovisiona la mensajería de JOBBI en LocalStack (SNS + SQS + DLQ).
#
# Es idempotente: se puede correr cuantas veces se quiera. Sirve en dos lugares:
#
#   - Dentro de LocalStack, como init hook (/etc/localstack/init/ready.d). Así se
#     ejecuta solo cada vez que LocalStack arranca, y como LocalStack no guarda
#     estado, un reinicio no deja al sistema sin tema ni cola. Es como lo usan
#     ./scripts/run-backend-local.sh (Docker) y k8s/localstack-deployment.yaml,
#     que lleva una copia de este script en un ConfigMap.
#   - Desde tu máquina, contra el port-forward: ./scripts/init-aws-local.sh
#
#   contratacion-completada-topic (SNS)
#        └──► monetizacion-events-queue (SQS)  ──5 fallos──► monetizacion-events-dlq
set -euo pipefail

# La región va por variable: awslocal toma su primer argumento como el servicio.
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
if command -v awslocal >/dev/null 2>&1; then
  AWS="awslocal"
else
  export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}" AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
  AWS="aws --endpoint-url=${ENDPOINT_URL:-http://localhost:4566}"
fi

TOPIC="contratacion-completada-topic"
QUEUE="monetizacion-events-queue"
DLQ="monetizacion-events-dlq"

echo "=== Aprovisionando SNS/SQS de JOBBI ==="

TOPIC_ARN=$($AWS sns create-topic --name "$TOPIC" --query TopicArn --output text)
echo "  ✓ Tema SNS: $TOPIC_ARN"

DLQ_URL=$($AWS sqs create-queue --queue-name "$DLQ" --query QueueUrl --output text)
DLQ_ARN=$($AWS sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)
echo "  ✓ DLQ:       $DLQ_ARN"

QUEUE_URL=$($AWS sqs create-queue --queue-name "$QUEUE" --query QueueUrl --output text)
QUEUE_ARN=$($AWS sqs get-queue-attributes --queue-url "$QUEUE_URL" \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)

# Un mensaje que falla 5 veces se aparta a la DLQ para no bloquear la cola.
# VisibilityTimeout: lo que tarda en reaparecer un mensaje cuyo proceso falló.
ATRIBUTOS=$(mktemp)
cat > "$ATRIBUTOS" <<EOF
{
  "VisibilityTimeout": "30",
  "RedrivePolicy": "{\"deadLetterTargetArn\":\"$DLQ_ARN\",\"maxReceiveCount\":\"5\"}"
}
EOF
$AWS sqs set-queue-attributes --queue-url "$QUEUE_URL" --attributes "file://$ATRIBUTOS"
rm -f "$ATRIBUTOS"
echo "  ✓ Cola SQS:  $QUEUE_ARN (DLQ tras 5 intentos)"

# subscribe es idempotente: con el mismo tema, protocolo y endpoint devuelve la
# suscripción existente.
SUB_ARN=$($AWS sns subscribe --topic-arn "$TOPIC_ARN" --protocol sqs \
  --notification-endpoint "$QUEUE_ARN" --query SubscriptionArn --output text)
echo "  ✓ Suscripción tema → cola: $SUB_ARN"

echo "=== Mensajería lista ==="
