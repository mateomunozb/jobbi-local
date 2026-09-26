#!/usr/bin/env bash
# Construye y despliega el sistema completo en Minikube: los 9 microservicios
# de dominio, el API Gateway y el frontend ya conectado al backend.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS="aws-local"

echo "=== 1/5 · Namespace e infraestructura emulada (LocalStack: SNS + SQS + DLQ) ==="
kubectl apply -f "$ROOT_DIR/k8s/localstack-deployment.yaml"

echo
echo "=== 2/5 · PostgreSQL (una base por contexto) ==="
kubectl apply -f "$ROOT_DIR/k8s/postgres-deployment.yaml"
# Los servicios reintentan la conexión al arrancar, pero esperar aquí evita
# ver los Pods en Pending mientras la base se inicializa por primera vez.
kubectl rollout status statefulset/postgres -n "$NS" --timeout=300s

echo
echo "=== 3/5 · Imágenes ==="
"$ROOT_DIR/scripts/build-backend-image.sh"
"$ROOT_DIR/scripts/build-frontend-image.sh"

echo
echo "=== 4/5 · Manifiestos ==="
kubectl apply -f "$ROOT_DIR/k8s/domain-services.yaml"
kubectl apply -f "$ROOT_DIR/k8s/monetizacion-deployment.yaml"
kubectl apply -f "$ROOT_DIR/k8s/gateway-deployment.yaml"
kubectl apply -f "$ROOT_DIR/k8s/frontend-deployment.yaml"

# `apply` no reinicia los Pods si el manifiesto no cambió, y como la etiqueta de
# imagen es siempre :v1, una imagen reconstruida no se recogería sola.
echo
echo "=== 5/5 · Reiniciando para tomar las imágenes nuevas ==="
DEPLOYS="identidad mercado contrataciones comunicacion confianza soporte adquisicion proteccion servicio-monetizacion api-gateway jobbi-frontend"
for deploy in $DEPLOYS; do kubectl rollout restart "deployment/$deploy" -n "$NS" >/dev/null; done
for deploy in $DEPLOYS; do kubectl rollout status "deployment/$deploy" -n "$NS" --timeout=180s | tail -1; done
# LocalStack no se reinicia (su imagen es pública), pero el cobro de comisiones
# depende de que haya terminado de crear el tema y la cola.
kubectl rollout status deployment/localstack -n "$NS" --timeout=180s | tail -1

echo
kubectl get pods -n "$NS"
cat <<'FIN'

=== Sistema desplegado ===

Abre dos túneles, cada uno en su propia terminal:

    kubectl port-forward svc/jobbi-frontend 3000:3000 -n aws-local
    kubectl port-forward svc/api-gateway 8080:8080 -n aws-local

Luego entra a  http://localhost:3000.

El sistema arranca sin ninguna cuenta ni ningún dato. Crea primero el
**prestador** (declara su oficio, su categoría y su tarifa) y después el
**demandante**, que ya podrá encontrarlo y abrir el chat.

Para dejarlo todo vacío otra vez:  ./scripts/reset-datos.sh

El túnel del gateway no lo necesita el navegador (el frontend habla con el
gateway por DNS interno del clúster); sirve para Swagger y para el smoke test,
que crea cuentas propias: no lo corras antes de una demostración en vivo.
FIN
