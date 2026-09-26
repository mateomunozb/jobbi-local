#!/usr/bin/env bash
set -euo pipefail

IMAGE="jobbi/frontend:v1"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Construyendo imagen del frontend JOBBI ($IMAGE) ==="
docker build -t "$IMAGE" "$ROOT_DIR/frontend"

echo "=== Cargando imagen en el registro interno de Minikube ==="
minikube image load "$IMAGE"

echo "=== Listo. Despliega con: kubectl apply -f k8s/frontend-deployment.yaml ==="
