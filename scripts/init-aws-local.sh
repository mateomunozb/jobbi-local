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
#   contratacion-completada-topic (SNS)   Contrataciones → Monetización
#        └──► monetizacion-events-queue (SQS)  ──5 fallos──► monetizacion-events-dlq
#   billetera-bloqueada-topic (SNS)       Monetización → Comunicación
#        └──► comunicacion-events-queue (SQS)  ──5 fallos──► comunicacion-events-dlq
set -euo pipefail

# La región va por variable: awslocal toma su primer argumento como el servicio.
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
if command -v awslocal >/dev/null 2>&1; then
  AWS="awslocal"
else
  export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}" AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
  AWS="aws --endpoint-url=${ENDPOINT_URL:-http://localhost:4566}"
fi

# aprovisionar TEMA COLA DLQ: tema SNS con una cola suscrita y su DLQ.
aprovisionar() {
  local topic="$1" queue="$2" dlq="$3"

  local topic_arn
  topic_arn=$($AWS sns create-topic --name "$topic" --query TopicArn --output text)
  echo "  ✓ Tema SNS: $topic_arn"

  local dlq_url dlq_arn
  dlq_url=$($AWS sqs create-queue --queue-name "$dlq" --query QueueUrl --output text)
  dlq_arn=$($AWS sqs get-queue-attributes --queue-url "$dlq_url" \
    --attribute-names QueueArn --query Attributes.QueueArn --output text)
  echo "  ✓ DLQ:       $dlq_arn"

  local queue_url queue_arn
  queue_url=$($AWS sqs create-queue --queue-name "$queue" --query QueueUrl --output text)
  queue_arn=$($AWS sqs get-queue-attributes --queue-url "$queue_url" \
    --attribute-names QueueArn --query Attributes.QueueArn --output text)

  # Un mensaje que falla 5 veces se aparta a la DLQ para no bloquear la cola.
  # VisibilityTimeout: lo que tarda en reaparecer un mensaje cuyo proceso falló.
  local atributos
  atributos=$(mktemp)
  cat > "$atributos" <<JSON
{
  "VisibilityTimeout": "30",
  "RedrivePolicy": "{\"deadLetterTargetArn\":\"$dlq_arn\",\"maxReceiveCount\":\"5\"}"
}
JSON
  $AWS sqs set-queue-attributes --queue-url "$queue_url" --attributes "file://$atributos"
  rm -f "$atributos"
  echo "  ✓ Cola SQS:  $queue_arn (DLQ tras 5 intentos)"

  # subscribe es idempotente: con el mismo tema, protocolo y endpoint devuelve la
  # suscripción existente.
  local sub_arn
  sub_arn=$($AWS sns subscribe --topic-arn "$topic_arn" --protocol sqs \
    --notification-endpoint "$queue_arn" --query SubscriptionArn --output text)
  echo "  ✓ Suscripción tema → cola: $sub_arn"
}

echo "=== Aprovisionando SNS/SQS de JOBBI ==="
aprovisionar contratacion-completada-topic monetizacion-events-queue monetizacion-events-dlq
aprovisionar billetera-bloqueada-topic comunicacion-events-queue comunicacion-events-dlq
echo "=== Mensajería lista ==="
