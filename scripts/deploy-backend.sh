#!/usr/bin/env bash
# Despliega todos los microservicios de dominio y el API Gateway en Minikube.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS="aws-local"

echo "=== Aplicando manifiestos ==="
# LocalStack crea el namespace y aprovisiona SNS/SQS al arrancar: el cobro de
# comisiones viaja como evento por ahí, así que se despliega junto al backend.
kubectl apply -f "$ROOT_DIR/k8s/localstack-deployment.yaml"

# PostgreSQL debe existir antes que los servicios: de ahí sale su DATABASE_URL.
kubectl apply -f "$ROOT_DIR/k8s/postgres-deployment.yaml"
kubectl rollout status statefulset/postgres -n "$NS" --timeout=300s

kubectl apply -f "$ROOT_DIR/k8s/domain-services.yaml"
kubectl apply -f "$ROOT_DIR/k8s/monetizacion-deployment.yaml"
kubectl apply -f "$ROOT_DIR/k8s/gateway-deployment.yaml"

echo "=== Esperando a que los Deployments estén disponibles ==="
for deploy in localstack identidad mercado contrataciones comunicacion confianza \
              soporte adquisicion proteccion servicio-monetizacion api-gateway; do
  kubectl rollout status "deployment/$deploy" -n "$NS" --timeout=120s
done

echo
kubectl get pods -n "$NS" -o wide
echo
echo "=== Backend desplegado ==="
echo
echo "Abre el gateway dejando esto corriendo en otra terminal:"
echo "    kubectl port-forward svc/api-gateway 8080:8080 -n $NS"
echo
echo "La base arranca vacía. Para volver a dejarla así en cualquier momento:"
echo "    ./scripts/reset-datos.sh"
echo
echo "El smoke test (./scripts/smoke-test-backend.sh) crea sus propias cuentas"
echo "y oficios: úsalo para verificar los endpoints, no antes de una demo."
echo
# En Linux el NodePort sí es alcanzable desde el host. Con el driver Docker en
# macOS y Windows no lo es, y `minikube service --url` abre un túnel que bloquea
# la terminal, así que ahí el camino es el port-forward de arriba.
if [ "$(uname -s)" = "Linux" ]; then
  echo "También puedes usar el NodePort directo: http://$(minikube ip):30080"
fi
