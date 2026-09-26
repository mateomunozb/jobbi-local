#!/usr/bin/env bash
# FALLO 4 · Recursos: el Pod que cobra comisiones agota su memoria (OOMKilled).
#
#   ./scripts/caos/fallo4-oomkilled.sh            # consumidor fuera ~30 s
#   CAIDA=45 ./scripts/caos/fallo4-oomkilled.sh   # más tiempo fuera
#
# Hipótesis: con `resources.limits.memory: 256Mi`, un consumo progresivo dentro
# del contenedor de Monetización lo lleva al límite; el kernel lo mata
# (memory.oom.group=1: muere el contenedor entero) y Kubernetes registra
# OOMKilled y lo reinicia. El fallo queda contenido en ese Pod: el resto del
# sistema sigue, los eventos esperan en SQS y se cobran al volver, sin
# pérdidas ni dobles cobros.
#
# Línea de tiempo (~6 min, carga nominal de 12 TPS, ~2 cobros por segundo):
#   0:00  línea base.
#   1:00  rampa: un proceso reserva 4 MiB por segundo dentro del contenedor (mismo
#         cgroup, cuenta contra el mismo límite). Tarda ~45 s en llegar a 256 Mi:
#         lo bastante lento para que cAdvisor (~10-15 s entre mediciones) la vea.
#         Mientras sube, el worker sigue cobrando: la cola no crece todavía.
#   ~1:45 OOMKilled. La fuga reaparece en cada arranque (se reinyecta apenas el
#         contenedor vuelve) y Kubernetes espera cada vez más para reiniciarlo
#         (CrashLoopBackOff: 0 s, 10 s, 20 s…). Así el consumidor queda fuera
#         ~CAIDA segundos y los eventos se ENCOLAN en SQS (visibles).
#   ~2:20 ya sin fuga, el contenedor vuelve, el worker se REANUDA y drena la cola.
#         Lo que tenía en la mano al morir queda en vuelo 30 s (VisibilityTimeout)
#         y luego se cobra; si ya estaba cobrado, el Idempotent Receiver lo descarta.
#
# Paneles (tablero 7): memoria % del límite, Cola de Monetización · visibles vs.
# en vuelo, Flujo del cobro (publicados vs. consumidos), reinicios, OOMKilled, DLQ.
set -euo pipefail
source "$(dirname "$0")/comun.sh"

CAIDA=${CAIDA:-30}

# Rampa lenta: 4 MiB tocados de verdad por segundo, hasta que el kernel lo mate.
RAMPA='
import time
bloques = []
while True:
    bloques.append(bytearray(4 * 1024 * 1024))
    time.sleep(1)'
# La fuga al arrancar: llega al límite en menos de un segundo.
FUGA_AL_ARRANCAR='
bloques = []
while True:
    bloques.append(bytearray(32 * 1024 * 1024))'

# "reinicios arrancado_en" del contenedor; arrancado_en vacío si no está corriendo.
estado() {
  kubectl get pod -n "$NS" "$POD" \
    -o jsonpath='{.status.containerStatuses[0].restartCount} {.status.containerStatuses[0].state.running.startedAt}'
}
cola() { prom 'max by (estado) (jobbi_sqs_mensajes{cola="monetizacion-events-queue"})'; }

iniciar_bitacora fallo4-oomkilled
carga_de_fondo nominal DURACION=6m
espera 60 "línea base"
foto "ANTES"

POD=$(kubectl get pod -n "$NS" -l app=servicio-monetizacion -o jsonpath='{.items[0].metadata.name}')
REINICIOS=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.containerStatuses[0].restartCount}')
marca "INYECCIÓN: rampa de 4 MiB/s en $POD (límite $(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.spec.containers[0].resources.limits.memory}'), reinicios $REINICIOS)"
INICIO=$SECONDS
kubectl exec -n "$NS" "$POD" -- python -c "$RAMPA" >/dev/null 2>&1 || true
CAIDO_DESDE=$SECONDS
marca "OOMKilled #1 tras $((CAIDO_DESDE - INICIO)) s de rampa · cola: $(cola)"

# Mientras no se cumpla CAIDA, cada vez que Kubernetes arranca el contenedor la
# fuga lo vuelve a matar. El tope de 8 evita un bucle si el backoff no crece.
MUERTES=1
ULTIMO=$REINICIOS
while :; do
  while :; do
    read -r R ARRANQUE <<<"$(estado)" || true
    [ "${R:-0}" -gt "$ULTIMO" ] && [ -n "${ARRANQUE:-}" ] && break
    sleep 0.3
  done
  ULTIMO=$R
  if [ $((SECONDS - CAIDO_DESDE)) -ge "$CAIDA" ] || [ "$MUERTES" -ge 8 ]; then break; fi
  kubectl exec -n "$NS" "$POD" -- python -c "$FUGA_AL_ARRANCAR" >/dev/null 2>&1 || true
  MUERTES=$((MUERTES + 1))
  marca "Reinicio $R a los $((SECONDS - CAIDO_DESDE)) s: la fuga reaparece → OOMKilled #$MUERTES · cola: $(cola)"
done
marca "Reinicio $ULTIMO a los $((SECONDS - CAIDO_DESDE)) s: ya sin fuga, el contenedor arranca · cola: $(cola)"

kubectl get events -n "$NS" --field-selector involvedObject.name="$POD" --sort-by=.lastTimestamp | tail -8 | tee -a "$BITACORA"
MOTIVO=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}')
marca "Última terminación del contenedor: ${MOTIVO:-desconocida} · reinicios: $ULTIMO"

kubectl wait --for=condition=Ready pod/"$POD" -n "$NS" --timeout=180s >/dev/null
marca "Contenedor listo: consumidor fuera ~$((SECONDS - CAIDO_DESDE)) s · cola: $(cola)"
espera 15 "el worker se reanuda y drena lo encolado"
marca "Cola tras el drenaje: $(cola)"
espera 45 "lo que quedó en vuelo reaparece al vencer su visibilidad (30 s) y se cobra"
foto "DESPUÉS"

esperar_carga
marca "Duplicados descartados por el Idempotent Receiver (logs del contenedor actual):"
kubectl logs -n "$NS" "$POD" --since=10m | grep -c evento_duplicado | tee -a "$BITACORA" || true
cerrar_con_integridad
[ "${MOTIVO:-}" = "OOMKilled" ]
