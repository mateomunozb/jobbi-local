#!/usr/bin/env bash
# Instala KEDA y activa el auto-escalado horizontal de Monetización por la cola SQS.
#
#   ./scripts/deploy-autoescalado.sh            # instala KEDA (si falta) y aplica el ScaledObject
#   ./scripts/deploy-autoescalado.sh --quitar   # retira el ScaledObject y deja 1 réplica fija
#
# KEDA (Kubernetes Event-Driven Autoscaling) lee la profundidad de la cola en
# LocalStack y le dice al HPA de Kubernetes cuántas réplicas hacen falta. La
# regla está en k8s/autoescalado/monetizacion-keda.yaml. Se instala en su propio
# namespace (`keda`, ~3 Pods, ~300 Mi) y queda instalado: quitar el
# ScaledObject basta para volver al comportamiento de siempre.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS="aws-local"
KEDA_VERSION="2.20.2"
MANIFIESTO="$ROOT_DIR/k8s/autoescalado/monetizacion-keda.yaml"

if [ "${1:-}" = "--quitar" ]; then
  echo "=== Retirando el auto-escalado de Monetización ==="
  kubectl delete -f "$MANIFIESTO" --ignore-not-found
  kubectl scale deploy/servicio-monetizacion -n "$NS" --replicas=1
  kubectl rollout status deploy/servicio-monetizacion -n "$NS" --timeout=180s
  echo "Monetización vuelve a 1 réplica fija. KEDA sigue instalado (namespace keda)."
  exit 0
fi

echo "=== 1/3 · KEDA ${KEDA_VERSION} ==="
if kubectl get deploy/keda-operator -n keda >/dev/null 2>&1; then
  echo "  ya instalado"
else
  # --server-side: los CRD de KEDA superan el límite de la anotación de `apply` clásico.
  kubectl apply --server-side \
    -f "https://github.com/kedacore/keda/releases/download/v${KEDA_VERSION}/keda-${KEDA_VERSION}.yaml"
fi
for deploy in keda-operator keda-metrics-apiserver keda-admission; do
  kubectl rollout status "deployment/$deploy" -n keda --timeout=300s
done

echo
echo "=== 2/3 · Regla de escalado (ScaledObject por la cola monetizacion-events-queue) ==="
# El webhook de KEDA valida el ScaledObject; recién instalado puede tardar unos
# segundos en aceptar peticiones.
for intento in $(seq 1 12); do
  kubectl apply -f "$MANIFIESTO" && break
  [ "$intento" = 12 ] && exit 1
  echo "  webhook de KEDA aún no disponible; reintento en 5 s…"; sleep 5
done

echo
echo "=== 3/3 · Esperando a que KEDA lea la cola ==="
for _ in $(seq 1 24); do
  listo=$(kubectl get scaledobject/monetizacion-cola -n "$NS" \
            -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
  [ "$listo" = "True" ] && break
  sleep 5
done
kubectl get scaledobject/monetizacion-cola -n "$NS"
kubectl get hpa/monetizacion-cola -n "$NS"
if [ "${listo:-}" != "True" ]; then
  echo "!!! El ScaledObject no quedó Ready. Revisa: kubectl describe scaledobject/monetizacion-cola -n $NS"
  exit 1
fi
cat <<'FIN'

=== Auto-escalado activo ===
Monetización escala de 1 a 3 réplicas según los mensajes que esperan en su cola.

Probarlo:   ./scripts/caos/autoescalado-cola.sh
Tablero:    Grafana → «11 · Auto-escalado horizontal de Monetización por la cola»
Mirarlo:    kubectl get hpa monetizacion-cola -n aws-local -w
FIN
