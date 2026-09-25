#!/usr/bin/env bash
# Despliega la observabilidad (Prometheus + Grafana + kube-state-metrics) en
# Minikube, en el namespace `monitoring`.
#
#   ./scripts/deploy-monitoring.sh
#
# Memoria: el backend ya ocupa ~2,4 GB y el stack suma ~0,5 GB, más lo que piden
# las pruebas de estrés. Con el driver Docker el contenedor de Minikube se amplía
# en caliente, sin perder datos (minikube start no lo recuerda: repítelo tras
# recrear el clúster):
#
#   docker update --memory 6g --memory-swap 6g minikube
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS="monitoring"

LIMITE=$(docker inspect minikube --format '{{.HostConfig.Memory}}' 2>/dev/null || echo 0)
if [ "$LIMITE" -gt 0 ] && [ "$LIMITE" -lt 5000000000 ]; then
  echo "!!! Minikube tiene $((LIMITE / 1024 / 1024)) MiB. Recomendado: docker update --memory 6g --memory-swap 6g minikube"
fi

echo "=== Aplicando k8s/monitoring ==="
kubectl apply -k "$ROOT_DIR/k8s/monitoring"

echo "=== Esperando a que el stack esté disponible ==="
for deploy in kube-state-metrics prometheus grafana; do
  kubectl rollout status "deployment/$deploy" -n "$NS" --timeout=300s
done

echo
kubectl get pods -n "$NS" -o wide
echo
echo "=== Observabilidad lista ==="
echo "Grafana     kubectl port-forward svc/grafana 3001:3000 -n $NS      → http://localhost:3001"
echo "            (tablero «JOBBI · Cobro de comisión en efectivo»; admin / jobbi para editar)"
echo "Prometheus  kubectl port-forward svc/prometheus 9090:9090 -n $NS   → http://localhost:9090"
echo "            (Status → Targets: todos UP · Alerts: reglas de SLO)"
echo "Logs JSON   kubectl logs -n aws-local deploy/servicio-monetizacion -f | grep business_event"
