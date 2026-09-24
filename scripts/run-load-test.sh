#!/usr/bin/env bash
# Pruebas de carga y estrés del Pub/Sub con k6.
#
#   ./scripts/run-load-test.sh [prueba] [escenario] [vus]
#
#   prueba     e2e  flujo completo por el gateway: check-out → evento → cobro (por defecto)
#              sns  solo el broker: publica directo en SNS y mide la ingesta del worker
#   escenario  humo | carga (por defecto) | estres | pico   (ver tests/k6/lib/escenarios.js)
#   vus        usuarios virtuales máximos (opcional; cada escenario trae su valor)
#
# Ejemplos:
#   ./scripts/run-load-test.sh e2e humo          # ¿funciona? (1 VU, 3 servicios)
#   ./scripts/run-load-test.sh e2e carga         # objetivos de servicio
#   ./scripts/run-load-test.sh e2e estres 80     # escalones hasta 80 VUs
#   ./scripts/run-load-test.sh e2e pico          # ráfaga repentina
#   ./scripts/run-load-test.sh sns estres
#
# Necesita los port-forwards de Minikube (o ./scripts/run-backend-local.sh):
#   e2e: kubectl port-forward svc/api-gateway 8080:8080 -n aws-local
#   sns: kubectl port-forward svc/localstack 4566:4566 -n aws-local
#        kubectl port-forward svc/servicio-monetizacion 8000:8000 -n aws-local
#
# Usa k6 si está instalado (brew install k6); si no, la imagen grafana/k6.
# El resumen queda en tests/k6/resultados/.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRUEBA="${1:-e2e}"
ESCENARIO="${2:-carga}"
VUS="${3:-}"

case "$PRUEBA" in
  e2e) SCRIPT="e2e-checkout-pubsub.js" ;;
  sns) SCRIPT="load-test-pubsub.js" ;;
  *) echo "Prueba desconocida '$PRUEBA' (usa e2e o sns)"; exit 1 ;;
esac

alcanzable() { curl -s -o /dev/null --max-time 3 "$1"; }
if [ "$PRUEBA" = "e2e" ]; then
  alcanzable http://localhost:8080/health || { echo "El gateway no responde en localhost:8080 (¿port-forward?)"; exit 1; }
else
  alcanzable http://localhost:4566/_localstack/health || { echo "LocalStack no responde en localhost:4566 (¿port-forward?)"; exit 1; }
  alcanzable http://localhost:8000/health || { echo "Monetización no responde en localhost:8000 (¿port-forward?)"; exit 1; }
fi

mkdir -p "$ROOT_DIR/tests/k6/resultados"
RESUMEN="resultados/${PRUEBA}-${ESCENARIO}-$(date +%Y%m%d-%H%M%S).json"
ARGS=(-e "ESCENARIO=$ESCENARIO" --summary-export "$RESUMEN")
[ -n "$VUS" ] && ARGS+=(-e "VUS=$VUS")

echo "=== k6 · prueba '$PRUEBA' · escenario '$ESCENARIO'${VUS:+ · $VUS VUs} ==="
cd "$ROOT_DIR/tests/k6"
set +e
if command -v k6 >/dev/null 2>&1; then
  k6 run "${ARGS[@]}" "$SCRIPT"
else
  echo "(k6 no está instalado: se usa la imagen grafana/k6)"
  # Dentro del contenedor, "localhost" es el contenedor: el host se alcanza
  # por host.docker.internal (el --add-host lo hace funcionar también en Linux).
  RED=(--add-host=host.docker.internal:host-gateway)
  SNS_URL=http://host.docker.internal:4566/
  # Con el backend local, LocalStack es un contenedor: k6 entra en su red y le
  # habla directo. Pasar por host.docker.internal y el puerto publicado suma
  # dos saltos de red que, a cientos de conexiones por segundo, dan timeouts
  # que son de Docker Desktop y no del sistema bajo prueba.
  if [ "$PRUEBA" = "sns" ] && docker inspect -f '{{.State.Running}}' jobbi-localstack 2>/dev/null | grep -q true; then
    RED=(--network container:jobbi-localstack)
    SNS_URL=http://localhost:4566/
  fi
  docker run --rm -i "${RED[@]}" \
    -v "$ROOT_DIR/tests/k6:/pruebas" -w /pruebas \
    -e BASE_URL=http://host.docker.internal:8080 \
    -e AWS_SNS_ENDPOINT="$SNS_URL" \
    -e MONETIZACION_URL=http://host.docker.internal:8000 \
    grafana/k6 run "${ARGS[@]}" "$SCRIPT"
fi
RESULTADO=$?
set -e

echo
echo "=== Estado del Pub/Sub tras la prueba ==="
if alcanzable http://localhost:8080/health; then
  curl -s http://localhost:8080/api/bff/pubsub/estado | jq '{
    outbox: {pendientes: .productor.pendientes, publicados: .productor.publicados,
             latenciaPublicacionMs: .productor.latenciaPublicacionMs},
    cola: .consumidor.colas.principal, dlq: .consumidor.colas.dlq,
    consumidor: .consumidor.porResultado,
    latenciaExtremoAExtremoMs: .consumidor.latenciaExtremoAExtremoMs}'
else
  curl -s http://localhost:8000/cobros/estado-worker | jq '{cobrosProcesados, porResultado, colas}'
fi
echo "Resumen k6: tests/k6/$RESUMEN"
exit $RESULTADO
