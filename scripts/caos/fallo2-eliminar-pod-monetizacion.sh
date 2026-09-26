#!/usr/bin/env bash
# FALLO 2 · Servicios: se elimina el Pod de Monetización mientras cobra comisiones.
#
#   ./scripts/caos/fallo2-eliminar-pod-monetizacion.sh [--forzado]
#
#   --forzado   borra con --grace-period=0 (muerte abrupta, como un nodo caído)
#               en vez de la terminación ordenada (SIGTERM) por defecto.
#
# Hipótesis: el Deployment recrea el Pod solo. Los mensajes que el Pod había
# recibido y no alcanzó a confirmar vuelven a la cola al vencer su visibilidad
# (30 s) y el Pod nuevo los procesa. Los que sí alcanzó a cobrar pero no a
# borrar de SQS llegan duplicados y el Idempotent Receiver los descarta: ni una
# comisión perdida ni una cobrada dos veces (RN-04).
#
# Línea de tiempo (~6 min): carga nominal (12 TPS, ~2 cobros por segundo, así
# que siempre hay eventos en vuelo); eliminación a 1:00 y de nuevo a 2:30;
# verificación de integridad. No se usa más carga: por encima de ~20 TPS se
# satura la CPU del gateway y ese efecto taparía el del fallo.
#
# Paneles: eventos consumidos (DUPLICADO sube tras cada muerte), colas SQS
# (sube y se drena), réplicas/reinicios, latencia del cobro asíncrono.
set -euo pipefail
source "$(dirname "$0")/comun.sh"

GRACIA=()
[ "${1:-}" = "--forzado" ] && GRACIA=(--grace-period=0 --force)

matar() {
  local pod
  pod=$(kubectl get pod -n "$NS" -l app=servicio-monetizacion -o jsonpath='{.items[0].metadata.name}')
  marca "INYECCIÓN: kubectl delete pod $pod ${GRACIA[*]:-}"
  kubectl delete pod -n "$NS" "$pod" "${GRACIA[@]}" --wait=false | tee -a "$BITACORA"
  local inicio=$SECONDS
  kubectl rollout status deploy/servicio-monetizacion -n "$NS" --timeout=180s >/dev/null
  kubectl wait --for=condition=Ready pod -l app=servicio-monetizacion -n "$NS" --timeout=180s >/dev/null
  marca "Pod de reemplazo listo en $((SECONDS - inicio)) s: $(kubectl get pod -n "$NS" -l app=servicio-monetizacion -o name)"
}

iniciar_bitacora fallo2-eliminar-pod-monetizacion
carga_de_fondo nominal DURACION=5m
espera 60 "línea base"
foto "ANTES"

matar
espera 20 "mensajes en vuelo vuelven a la cola al vencer su visibilidad (30 s)"
foto "DURANTE (tras la primera eliminación)"
espera 50 "drenaje"

matar
espera 60 "drenaje tras la segunda eliminación"
foto "DESPUÉS"

esperar_carga
marca "Duplicados descartados por el Idempotent Receiver (logs del Pod nuevo):"
kubectl logs -n "$NS" deploy/servicio-monetizacion --since=10m | grep -c evento_duplicado | tee -a "$BITACORA" || true
cerrar_con_integridad
