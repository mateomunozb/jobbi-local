#!/usr/bin/env bash
# Corre una prueba k6 como Job de Kubernetes, dentro de Minikube.
#
#   ./scripts/k6-en-cluster.sh <escenario> [VAR=valor ...]
#
#   escenario   nominal | pico50 | quiebre | resistencia   (perfiles por TPS, punto 10.4)
#               humo | carga | estres | pico                (perfiles por VUs, anteriores)
#   VAR=valor   variables para k6 (-e): TPS=20, DURACION=10m, PRESTADORES=10, …
#
# Ejemplos:
#   ./scripts/k6-en-cluster.sh humo                          # ¿funciona el camino completo?
#   ./scripts/k6-en-cluster.sh nominal                       # 12 TPS · 5 min · SLO p95 < 300 ms
#   ./scripts/k6-en-cluster.sh pico50                        # ráfaga de 50 TPS
#   ./scripts/k6-en-cluster.sh quiebre                       # 75 → 125 → 250 → 350 TPS (~13 min)
#   ./scripts/k6-en-cluster.sh resistencia DURACION=2h       # 12 TPS sostenidos
#
# Por qué dentro del clúster: desde el host, el tráfico pasa por un
# `kubectl port-forward`, que se satura mucho antes que el sistema y falsea el
# punto de quiebre. Aquí k6 habla con el Service del gateway por la red del
# clúster. Minikube necesita CPU para ambos: docker update --cpus 6 minikube.
#
# El resumen queda en tests/k6/resultados/<escenario>-<fecha>.json; mientras
# corre, el tablero de Grafana muestra el efecto en el lado del servidor.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ESCENARIO="${1:?Uso: $0 <escenario> [VAR=valor ...]}"
shift
NS="pruebas"
TRABAJO="k6-${ESCENARIO}-$(date +%H%M%S)"
RESUMEN="$ROOT_DIR/tests/k6/resultados/${ESCENARIO}-$(date +%Y%m%d-%H%M%S).json"

ARGS="-e ESCENARIO=${ESCENARIO} -e BASE_URL=http://api-gateway.aws-local.svc.cluster.local:8080"
for variable in "$@"; do ARGS="$ARGS -e $variable"; done

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# Los scripts viajan en un ConfigMap; la clave no admite "/", así que la
# librería se monta de vuelta en lib/ con `items`.
kubectl create configmap k6-scripts -n "$NS" \
  --from-file=e2e-checkout-pubsub.js="$ROOT_DIR/tests/k6/e2e-checkout-pubsub.js" \
  --from-file=escenarios.js="$ROOT_DIR/tests/k6/lib/escenarios.js" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl apply -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${TRABAJO}
  namespace: ${NS}
  labels: { app: k6, escenario: ${ESCENARIO} }
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 10800
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels: { app: k6, escenario: ${ESCENARIO} }
    spec:
      restartPolicy: Never
      containers:
      - name: k6
        image: grafana/k6:0.54.0
        command: ["sh", "-c"]
        # El resumen se imprime entre marcas para recuperarlo de los logs.
        args:
        - >-
          k6 run ${ARGS} --summary-export /tmp/resumen.json /scripts/e2e-checkout-pubsub.js;
          codigo=\$?; echo '===RESUMEN-K6==='; cat /tmp/resumen.json; echo; echo '===FIN-RESUMEN==='; exit \$codigo
        resources:
          requests: { cpu: "500m", memory: "256Mi" }
          limits: { cpu: "2", memory: "1536Mi" }
        volumeMounts:
        - name: scripts
          mountPath: /scripts
      volumes:
      - name: scripts
        configMap:
          name: k6-scripts
          items:
          - { key: e2e-checkout-pubsub.js, path: e2e-checkout-pubsub.js }
          - { key: escenarios.js, path: lib/escenarios.js }
EOF

echo "=== k6 · escenario '${ESCENARIO}' · Job ${NS}/${TRABAJO} ==="
kubectl wait --for=condition=Ready pod -l job-name="$TRABAJO" -n "$NS" --timeout=180s >/dev/null 2>&1 || true
kubectl logs -f "job/$TRABAJO" -n "$NS" | sed '/===RESUMEN-K6===/,$d' || true

# Espera a que el Job termine (éxito o umbrales incumplidos) y guarda el resumen.
while ! kubectl get job "$TRABAJO" -n "$NS" -o jsonpath='{.status.conditions[*].type}' | grep -qE 'Complete|Failed'; do
  sleep 2
done
mkdir -p "$(dirname "$RESUMEN")"
kubectl logs "job/$TRABAJO" -n "$NS" | sed -n '/===RESUMEN-K6===/,/===FIN-RESUMEN===/p' | sed '1d;$d' > "$RESUMEN"
ESTADO=$(kubectl get job "$TRABAJO" -n "$NS" -o jsonpath='{.status.conditions[0].type}')
echo
echo "Resumen: ${RESUMEN#"$ROOT_DIR"/}"
if [ "$ESTADO" = "Complete" ]; then
  echo "=== k6 terminó: umbrales cumplidos ==="
else
  echo "=== k6 terminó con umbrales incumplidos (esperable en pico50/quiebre: ver el resumen) ==="
  exit 1
fi
