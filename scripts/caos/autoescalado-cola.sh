#!/usr/bin/env bash
# AUTO-ESCALADO · Monetización pasa de 1 a 2 y de 2 a 3 réplicas según su cola.
#
#   ./scripts/deploy-autoescalado.sh      # una vez: instala KEDA y la regla
#   ./scripts/caos/autoescalado-cola.sh
#
# Hipótesis: cuando llegan más cobros de los que un Pod procesa, los mensajes
# se acumulan en `monetizacion-events-queue`; KEDA lo ve y el HPA agrega un Pod
# cada vez que hay más de 100 mensajes esperando por Pod (máximo 3). Con más
# consumidores la cola se drena. Al pasar la carga, las réplicas vuelven a 1,
# de una en una. Durante todo el proceso no se pierde ni se duplica ningún
# cobro, aunque varios Pods lean la misma cola.
#
# Para que se sature un solo Pod con una carga que Minikube aguanta, durante la
# prueba Monetización corre con 1 hilo y 125 ms de trabajo simulado por mensaje
# (~7 mensajes/s por Pod). Al terminar (o si se interrumpe) vuelve a lo normal.
#
# Línea de tiempo (~13 min): nominal de fondo (12 TPS, ~2 cobros reales/s) más
# eventos publicados directo en SNS por escalones:
#   0:00-1:00   total ~4/s    → 1 réplica
#   1:00-3:30   total ~10/s   → 1 → 2 réplicas
#   3:30-6:30   total ~18/s   → 2 → 3 réplicas
#   6:30-       sin carga     → 3 → 2 → 1 (1 por minuto, tras 3 min de calma)
#
# Tablero: «11 · Auto-escalado horizontal de Monetización por la cola».
set -euo pipefail
source "$(dirname "$0")/comun.sh"

DEPLOY="servicio-monetizacion"
HPA="monetizacion-cola"
CORRIDA="$(date +%H%M%S)"

if [ "$(kubectl get scaledobject/$HPA -n "$NS" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" != "True" ]; then
  echo "El auto-escalado no está activo. Instálalo primero con: ./scripts/deploy-autoescalado.sh"
  exit 1
fi

replicas() { kubectl get deploy/$DEPLOY -n "$NS" -o jsonpath='{.status.readyReplicas}/{.spec.replicas}'; }
deseadas() { kubectl get hpa/$HPA -n "$NS" -o jsonpath='{.status.desiredReplicas}'; }
en_cola() {
  kubectl exec -n "$NS" deploy/localstack -- awslocal sqs get-queue-attributes \
    --queue-url "http://localhost:4566/000000000000/monetizacion-events-queue" \
    --attribute-names ApproximateNumberOfMessages \
    --query Attributes.ApproximateNumberOfMessages --output text 2>/dev/null || echo "?"
}

# Una línea por observación; marca en la bitácora cada vez que cambian las réplicas.
ULTIMAS=""
observar() {
  local actuales
  actuales=$(replicas)
  if [ "$actuales" != "$ULTIMAS" ]; then
    marca "RÉPLICAS listas/pedidas: $actuales · HPA desea $(deseadas) · mensajes esperando: $(en_cola)"
    ULTIMAS="$actuales"
  else
    echo "   · réplicas $actuales · HPA desea $(deseadas) · esperando $(en_cola)"
  fi
}

foto_escalado() {
  marca "── Auto-escalado: $1"
  {
    echo "   réplicas listas/pedidas        $(replicas)   (HPA desea $(deseadas))"
    echo "   mensajes esperando en la cola  $(en_cola)"
    echo "   consumo por Pod (msg/s)        $(prom 'sum by (pod) (rate(jobbi_eventos_consumidos_total{consumidor="monetizacion"}[1m]))')"
    echo "   p95 cobro real (s)             $(prom 'histogram_quantile(0.95, sum by (le) (rate(jobbi_cobro_latencia_seconds_bucket[1m])))')"
  } | tee -a "$BITACORA"
}

restaurar() {
  marca "RESTAURACIÓN: Monetización vuelve a 2 hilos y sin trabajo simulado"
  kubectl annotate scaledobject/$HPA -n "$NS" autoscaling.keda.sh/paused-replicas- >/dev/null 2>&1 || true
  kubectl set env deploy/$DEPLOY -n "$NS" SQS_HILOS=2 SQS_COSTO_MS- >/dev/null 2>&1 || true
}
trap restaurar EXIT

iniciar_bitacora autoescalado-cola

marca "PREPARACIÓN: Monetización con 1 hilo y 125 ms por mensaje (~7 msg/s por Pod)"
kubectl annotate scaledobject/$HPA -n "$NS" autoscaling.keda.sh/paused-replicas- >/dev/null 2>&1 || true
kubectl set env deploy/$DEPLOY -n "$NS" SQS_HILOS=1 SQS_COSTO_MS=125 >/dev/null
kubectl rollout status deploy/$DEPLOY -n "$NS" --timeout=300s >/dev/null
for _ in $(seq 1 40); do [ "$(replicas)" = "1/1" ] && break; sleep 15; done
marca "Punto de partida: $(replicas) réplica(s), $(en_cola) mensajes esperando"

carga_de_fondo nominal DURACION=7m
marca "Carga por escalones: publica en SNS 2 → 8 → 16 eventos/s (corrida AUTO-$CORRIDA)"
GUION=autoescalado-cola.js "$RAIZ/scripts/k6-en-cluster.sh" autoescalado "CORRIDA=$CORRIDA" \
  > "$BITACORA.escalones.txt" 2>&1 &
ESCALONES_PID=$!

inicio=$SECONDS
fotos_pendientes=("55:ANTES · escalón bajo (1 réplica)" "205:FIN DEL ESCALÓN MEDIO (se esperan 2)" "385:FIN DEL ESCALÓN ALTO (se esperan 3)")
while kill -0 "$ESCALONES_PID" 2>/dev/null; do
  observar
  if [ ${#fotos_pendientes[@]} -gt 0 ] && [ $((SECONDS - inicio)) -ge "${fotos_pendientes[0]%%:*}" ]; then
    foto_escalado "${fotos_pendientes[0]#*:}"
    fotos_pendientes=("${fotos_pendientes[@]:1}")
  fi
  sleep 15
done
wait "$ESCALONES_PID" || true
publicados=$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["metrics"]["autoescalado_publicados"]["count"]))' \
  "$(ls -t "$RAIZ"/tests/k6/resultados/autoescalado-*.json | head -1)" 2>/dev/null || echo "?")
marca "Terminó la carga por escalones: $publicados eventos publicados en SNS"

marca "Esperando a que las réplicas vuelvan a 1 (3 min de calma y luego 1 Pod por minuto; máx. 8 min)"
for _ in $(seq 1 32); do
  observar
  [ "$(replicas)" = "1/1" ] && break
  sleep 15
done
foto_escalado "DESPUÉS · de vuelta a 1 réplica"

esperar_carga

marca "Consistencia de la corrida: publicados en SNS vs. registrados por Monetización (esperando a que drene)"
procesados=0
for _ in $(seq 1 24); do
  procesados=$(psql_en monetizacion "SELECT count(*) FROM eventos_procesados WHERE \"contratacionId\" LIKE 'AUTO-$CORRIDA-%'")
  [ "$procesados" = "$publicados" ] && break
  sleep 5
done
distintos=$(psql_en monetizacion "SELECT count(DISTINCT \"contratacionId\") FROM eventos_procesados WHERE \"contratacionId\" LIKE 'AUTO-$CORRIDA-%'")
marca "   publicados $publicados · procesados $procesados · distintos $distintos"
[ "$procesados" = "$publicados" ] && [ "$distintos" = "$publicados" ] \
  && marca "   ✓ cada evento se procesó exactamente una vez" \
  || marca "   ✗ la corrida no cuadra (revisa la DLQ y la bitácora)"

marca "Decisiones del HPA durante la prueba:"
kubectl get events -n "$NS" --field-selector involvedObject.name=$HPA,reason=SuccessfulRescale \
  -o custom-columns=HORA:.lastTimestamp,DECISION:.message --no-headers 2>/dev/null | tail -8 | tee -a "$BITACORA" || true

cerrar_con_integridad
