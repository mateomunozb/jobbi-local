#!/usr/bin/env bash
# FALLO 4 · Recursos: el Pod que cobra comisiones agota su memoria (OOMKilled).
#
#   ./scripts/caos/fallo4-oomkilled.sh
#
# Hipótesis: con `resources.limits.memory: 256Mi`, un consumo progresivo dentro
# del contenedor de Monetización lo lleva al límite; el kernel lo mata
# (memory.oom.group=1: muere el contenedor entero) y Kubernetes registra
# OOMKilled y lo reinicia. El fallo queda contenido en ese Pod: el resto del
# sistema sigue, los eventos esperan en SQS y se cobran al volver, sin
# pérdidas ni dobles cobros.
#
# El consumo se inyecta con `kubectl exec`: un proceso que reserva 8 MiB cada
# 0,3 s dentro del mismo cgroup del contenedor (la memoria cuenta contra el
# mismo límite que la del servicio).
#
# Paneles: memoria por Pod y % del límite (sube en rampa y cae a cero),
# reinicios, OOMKilled, colas SQS.
set -euo pipefail
source "$(dirname "$0")/comun.sh"

iniciar_bitacora fallo4-oomkilled
carga_de_fondo nominal DURACION=5m
espera 60 "línea base"
foto "ANTES"

POD=$(kubectl get pod -n "$NS" -l app=servicio-monetizacion -o jsonpath='{.items[0].metadata.name}')
REINICIOS=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}')
marca "INYECCIÓN: consumo progresivo de memoria en $POD (límite $(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.spec.containers[0].resources.limits.memory}'), reinicios $REINICIOS)"
INICIO=$SECONDS
kubectl exec -n "$NS" "$POD" -- python -c '
import time
bloques = []
while True:
    bloques.append(bytearray(8 * 1024 * 1024))   # 8 MiB tocados de verdad
    time.sleep(0.3)' >/dev/null 2>&1 || true
marca "El proceso de consumo terminó tras $((SECONDS - INICIO)) s (el kernel mató el contenedor)"

for _ in $(seq 1 30); do
  MOTIVO=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}' 2>/dev/null || true)
  [ -n "$MOTIVO" ] && break
  sleep 2
done
marca "Última terminación del contenedor: ${MOTIVO:-desconocida} · reinicios: $(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
kubectl get events -n "$NS" --field-selector involvedObject.name="$POD" --sort-by=.lastTimestamp | tail -5 | tee -a "$BITACORA"
foto "DURANTE (tras el OOMKilled)"

kubectl wait --for=condition=Ready pod/"$POD" -n "$NS" --timeout=180s >/dev/null
marca "Contenedor de nuevo listo $((SECONDS - INICIO)) s después de iniciar la inyección"
espera 60 "drenaje de lo que llegó mientras estaba caído"
foto "DESPUÉS"

esperar_carga
cerrar_con_integridad
[ "${MOTIVO:-}" = "OOMKilled" ]
