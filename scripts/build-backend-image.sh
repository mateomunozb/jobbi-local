#!/usr/bin/env bash
# Construye la imagen única del backend y la carga en el registro de Minikube.
set -euo pipefail

IMAGE="jobbi/backend:v1"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Construyendo imagen del backend JOBBI ($IMAGE) ==="
docker build -t "$IMAGE" "$ROOT_DIR/services"

echo "=== Cargando imagen en el registro interno de Minikube ==="
minikube image load "$IMAGE"

echo "=== Listo. Despliega con: ./scripts/deploy-backend.sh ==="
