#!/usr/bin/env bash
# FALLO 3 · Base de datos: saturación temporal y auto-escalado en caliente.
#
#   ./scripts/caos/fallo3-saturacion-bd.sh
#
# Simula el auto-escalado de Aurora Serverless v2 (LocalStack no lo emula): el
# escalador (k8s/escalador-bd.yaml) mide la CPU y la memoria de PostgreSQL y le
# sube o baja la capacidad EN CALIENTE, sin reiniciar el Pod.
#
# Hipótesis: al saturar la base, su capacidad sube por escalones (0,5 → 1 → 2 →
# 4 ACU) hasta que la CPU usada queda por debajo del 75 % de la asignada; el
# check-out puede ponerse lento mientras la base está saturada y se recupera al
# darle capacidad. Al pasar la saturación, la capacidad baja escalón por
# escalón hasta 0,5 ACU. PostgreSQL no se reinicia en ningún momento, no se
# cortan las conexiones y el dinero cuadra.
#
# Línea de tiempo (~11 min):
#   0:00  carga nominal (12 TPS)                  → foto ANTES (0,5 ACU)
#   1:00  saturación moderada: 1 tps de pgbench   → sube a 1 ACU        → foto
#   3:30  pico: 4 tps de pgbench                  → sube a 2 y a 4 ACU  → foto
#   6:00  fin de la saturación                    → baja 4 → 2 → 1 → 0,5 ACU
#   9:30  foto DESPUÉS, historial de escalados, reinicios de PostgreSQL
#  ~11:00 fin de la carga                         → verificación de integridad
#
# Paneles (tablero «6 · Fallo 3»): capacidad en ACU, CPU y memoria usadas vs.
# asignadas (escalones), % de la CPU asignada en uso, throttling, latencia.
set -euo pipefail
source "$(dirname "$0")/comun.sh"

FASE_MODERADA="1:150"
FASE_PICO="4:150"
ESCALADOR="http://escalador-bd.aws-local.svc.cluster.local:8011"

restaurar() { kubectl delete job saturador-bd -n "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap restaurar EXIT

reinicios_bd() { kubectl get pod postgres-0 -n "$NS" -o jsonpath='{.status.containerStatuses[0].restartCount}'; }
capacidad() {
  kubectl get pod postgres-0 -n "$NS" -o jsonpath='{.status.containerStatuses[0].resources.limits}'
}

if ! kubectl rollout status deploy/escalador-bd -n "$NS" --timeout=60s >/dev/null 2>&1; then
  echo "El escalador no está desplegado: kubectl apply -f k8s/escalador-bd.yaml" >&2
  exit 1
fi

iniciar_bitacora fallo3-saturacion-bd
restaurar
REINICIOS_ANTES=$(reinicios_bd)
marca "PostgreSQL: reinicios=$REINICIOS_ANTES · recursos aplicados $(capacidad)"
carga_de_fondo nominal DURACION=11m
espera 60 "línea base con la capacidad mínima"
foto "ANTES (0,5 ACU)"

marca "INYECCIÓN: saturación moderada (pgbench ${FASE_MODERADA%%:*} tps) y luego pico (${FASE_PICO%%:*} tps)"
sed "s/__FASES__/$FASE_MODERADA $FASE_PICO/" "$(dirname "$0")/saturador-bd.yaml" | kubectl apply -f - | tee -a "$BITACORA"
espera 150 "saturación moderada: la CPU pasa del 75 % de la asignada y la base sube de nivel"
foto "SATURACIÓN MODERADA"
marca "PostgreSQL: recursos aplicados $(capacidad)"
espera 150 "pico: la base sube escalón por escalón hasta que la CPU usada baja del 75 %"
foto "PICO"
marca "PostgreSQL: recursos aplicados $(capacidad)"

marca "FIN DE LA SATURACIÓN: termina pgbench"
restaurar
espera 210 "la capacidad baja un escalón cada ~35 s de uso bajo, hasta 0,5 ACU"
foto "DESPUÉS (vuelta a la capacidad mínima)"
marca "PostgreSQL: recursos aplicados $(capacidad)"

marca "Historial de escalados (escalador-bd):"
en_cluster GET "$ESCALADOR/estado" | python3 -c '
import json, sys
for e in json.load(sys.stdin).get("historial", []):
    e["h"] = e["hora"][11:19]
    print("   {h} UTC  {direccion:6}  {deAcu:>3} → {aAcu:<3} ACU  CPU {cpuAsignada:g} núcleos · "
          "memoria {memoriaAsignadaMi} Mi · work_mem {workMemMb} MB  "
          "({motivo}; usaba {cpuUsada} núcleos y {memoriaUsadaMi} Mi)".format(**e))' | tee -a "$BITACORA"
REINICIOS_DESPUES=$(reinicios_bd)
marca "Reinicios de PostgreSQL: antes $REINICIOS_ANTES · después $REINICIOS_DESPUES (deben ser iguales: escalado en caliente)"

esperar_carga
cerrar_con_integridad
[ "$REINICIOS_ANTES" = "$REINICIOS_DESPUES" ]
