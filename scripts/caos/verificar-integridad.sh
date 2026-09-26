#!/usr/bin/env bash
# Verifica la integridad del dinero después de una prueba o un fallo inyectado.
#
#   ./scripts/caos/verificar-integridad.sh ["AAAA-MM-DD HH:MM:SS" UTC desde]
#
# Comprobaciones (todas deben dar 0):
#   1. Cobro doble: más de un cargo COMISION por contratación (RN-04, Idempotent Receiver).
#   2. Pago doble: más de un Pago por contratación.
#   3. Saldo descuadrado: saldoPendiente distinto de la suma de sus movimientos.
#   4. Comisión perdida: contratación completada en efectivo (desde la fecha) sin
#      su cargo en la billetera. Se espera hasta 120 s la consistencia eventual.
#   5. Outbox pendiente: eventos que no salieron hacia SNS.
#   6. DLQ: mensajes apartados tras 5 intentos.
set -uo pipefail

DESDE="${1:-1970-01-01 00:00:00}"
NS="aws-local"
psql_en() { kubectl exec -n "$NS" postgres-0 -- psql -U jobbi -d "jobbi_$1" -tAc "$2"; }
FALLAS=0
resultado() {  # nombre valor
  if [ "$2" = "0" ]; then echo "   ✓ $1: 0"; else echo "   ✗ $1: $2"; FALLAS=$((FALLAS + 1)); fi
}

resultado "cobros dobles" "$(psql_en monetizacion "SELECT count(*) FROM (SELECT \"contratacionId\" FROM movimientos WHERE tipo = 'COMISION' GROUP BY 1 HAVING count(*) > 1) d")"
resultado "pagos dobles" "$(psql_en monetizacion "SELECT count(*) FROM (SELECT \"contratacionId\" FROM pagos GROUP BY 1 HAVING count(*) > 1) d")"
resultado "billeteras con saldo descuadrado" "$(psql_en monetizacion "
  SELECT count(*) FROM billeteras b
  LEFT JOIN (SELECT \"billeteraId\", -sum(monto) AS suma FROM movimientos GROUP BY 1) m ON m.\"billeteraId\" = b.id
  WHERE abs(b.\"saldoPendiente\" - coalesce(m.suma, 0)) > 0.01")"

# Comisiones perdidas: cruza dos bases (no hay JOIN posible entre contextos).
COMPLETADAS=$(mktemp); COBRADAS=$(mktemp)
perdidas=""
for _ in $(seq 1 24); do
  psql_en contrataciones "SELECT id FROM contrataciones WHERE estado = 'COMPLETADA' AND \"medioPago\" = 'EFECTIVO'
                          AND \"checkOut\" >= '$DESDE' ORDER BY 1" > "$COMPLETADAS"
  psql_en monetizacion "SELECT DISTINCT \"contratacionId\" FROM movimientos WHERE tipo = 'COMISION' ORDER BY 1" > "$COBRADAS"
  perdidas=$(comm -23 <(sort "$COMPLETADAS") <(sort "$COBRADAS") | grep -c . || true)
  [ "$perdidas" = "0" ] && break
  sleep 5   # consistencia eventual: el cobro puede venir en camino
done
echo "   · contrataciones completadas en efectivo desde $DESDE: $(grep -c . "$COMPLETADAS" || true)"
resultado "comisiones perdidas (completadas sin cobro tras 120 s)" "$perdidas"
rm -f "$COMPLETADAS" "$COBRADAS"

resultado "eventos pendientes en outbox (contrataciones)" "$(psql_en contrataciones "SELECT count(*) FROM outbox_eventos WHERE estado = 'PENDIENTE'")"
resultado "eventos pendientes en outbox (monetización)" "$(psql_en monetizacion "SELECT count(*) FROM outbox_monetizacion WHERE estado = 'PENDIENTE'")"

for dlq in monetizacion-events-dlq comunicacion-events-dlq; do
  n=$(kubectl exec -n "$NS" deploy/localstack -- awslocal sqs get-queue-attributes \
        --queue-url "http://localhost:4566/000000000000/$dlq" \
        --attribute-names ApproximateNumberOfMessages \
        --query Attributes.ApproximateNumberOfMessages --output text 2>/dev/null || echo "?")
  resultado "mensajes en $dlq" "$n"
done

exit $((FALLAS > 0))
