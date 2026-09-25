#!/usr/bin/env bash
# Utilidades compartidas por los experimentos de fallos (se incluye con `source`).
#
# Cada experimento sigue el mismo guion:
#   1. carga nominal de fondo (k6 dentro del clúster) para que haya tráfico real;
#   2. foto de las métricas antes, durante y después del fallo (Prometheus);
#   3. inyección del fallo y restauración (con trap: se restaura aunque se corte);
#   4. verificación de integridad del dinero (verificar-integridad.sh).
# Todo queda en tests/caos/resultados/<experimento>-<fecha>.log.

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS="aws-local"
RESULTADOS="$RAIZ/tests/caos/resultados"
mkdir -p "$RESULTADOS"

iniciar_bitacora() {
  BITACORA="$RESULTADOS/$1-$(date +%Y%m%d-%H%M%S).log"
  # Hora UTC de inicio: las fechas del backend se guardan con la hora del Pod (UTC).
  INICIO_UTC="$(date -u '+%Y-%m-%d %H:%M:%S')"
  : > "$BITACORA"
  marca "Experimento '$1' · bitácora ${BITACORA#"$RAIZ"/}"
}

marca() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$BITACORA"; }

# Consulta instantánea a Prometheus; imprime "etiqueta=valor" por serie.
read -r -d '' _LEER_PROM <<'PY' || true
import json, sys
try:
    series = json.load(sys.stdin)["data"]["result"]
except Exception:
    print("sin dato"); sys.exit()
def nombre(m):
    return (m.get("pod") or m.get("app") or m.get("service") or m.get("resultado")
            or m.get("cola") or m.get("dependencia") or m.get("estado") or "")
partes = []
for s in series:
    valor = f"{float(s['value'][1]):.4g}"
    etiqueta = nombre(s["metric"])
    partes.append(f"{etiqueta}={valor}" if etiqueta else valor)
print("  ".join(partes) or "sin dato")
PY
prom() {
  local consulta
  consulta=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")
  kubectl exec -n monitoring deploy/prometheus -- \
    wget -qO- "http://localhost:9090/api/v1/query?query=$consulta" 2>/dev/null | python3 -c "$_LEER_PROM"
}

# Foto de las métricas del caso de uso. $1: rótulo (ANTES, DURANTE, DESPUÉS…).
foto() {
  marca "── Métricas: $1"
  local G='jobbi_http_request_duration_seconds'
  {
    echo "   rate gateway (req/s)           $(prom "sum(rate(${G}_count{service=\"gateway\"}[1m]))")"
    echo "   errores 5xx % por servicio     $(prom 'jobbi:http_errores:porcentaje > 0')"
    echo "   p95 gateway (s)                $(prom "histogram_quantile(0.95, sum by (le) (rate(${G}_bucket{service=\"gateway\"}[1m])))")"
    echo "   p95 registro (s)               $(prom "histogram_quantile(0.95, sum by (le) (rate(${G}_bucket{service=\"gateway\",route=\"/api/auth/registro\"}[1m])))")"
    echo "   prestadores por verificación   $(prom 'sum by (estado) (jobbi_prestadores_verificacion)')"
    echo "   p95 check-out (s)              $(prom "histogram_quantile(0.95, sum by (le) (rate(${G}_bucket{service=\"gateway\",route=\"/api/bff/contrataciones/{contratacion_id}/check-out\"}[1m])))")"
    echo "   p95 cobro asíncrono (s)        $(prom 'histogram_quantile(0.95, sum by (le) (rate(jobbi_cobro_latencia_seconds_bucket[1m])))')"
    echo "   eventos consumidos/min         $(prom 'sum by (resultado) (rate(jobbi_eventos_consumidos_total{consumidor="monetizacion"}[1m])) * 60')"
    echo "   outbox pendientes              $(prom 'sum(jobbi_outbox_pendientes)')"
    echo "   colas SQS (visibles)           $(prom 'max by (cola) (jobbi_sqs_mensajes{estado="visibles"})')"
    echo "   colas SQS (en vuelo)           $(prom 'max by (cola) (jobbi_sqs_mensajes{estado="en_vuelo"})')"
    echo "   eventos publicados/min         $(prom 'sum by (tema) (rate(jobbi_eventos_publicados_total[1m])) * 60')"
    echo "   recaudo %                      $(prom 'jobbi:recaudo_comision_efectivo:porcentaje')"
    echo "   circuito verificación          $(prom 'jobbi_circuit_breaker_estado')   (0 cerrado · 1 semiabierto · 2 abierto)"
    echo "   pool BD en uso                 $(prom 'sum by (app) (jobbi_db_pool_conexiones{estado="en_uso"}) > 0')"
    echo "   conexiones PostgreSQL          $(prom 'sum(jobbi_postgres_conexiones)') de $(prom 'max(jobbi_postgres_conexiones_max)')"
    echo "   capacidad BD (ACU)             $(prom 'max(jobbi_bd_capacidad_acu)')"
    echo "   CPU BD usada / asignada        $(prom 'sum(rate(container_cpu_usage_seconds_total{namespace="aws-local",container="postgres"}[30s]))') / $(prom 'max(kube_pod_container_resource_limits{namespace="aws-local",container="postgres",resource="cpu"})') núcleos"
    echo "   memoria BD usada / asignada    $(prom 'max(container_memory_working_set_bytes{namespace="aws-local",container="postgres"}) / 2^20') / $(prom 'max(kube_pod_container_resource_limits{namespace="aws-local",container="postgres",resource="memory"}) / 2^20') MiB"
    # Solo el contenedor más reciente: tras un OOMKilled cAdvisor mantiene ~5 min la serie del muerto.
    echo "   memoria % límite monetización  $(prom '100 * sum by (pod) (container_memory_working_set_bytes{namespace="aws-local",container="monetizacion"} and on (id) (container_start_time_seconds{namespace="aws-local",container="monetizacion"} == on (pod) group_left () max by (pod) (container_start_time_seconds{namespace="aws-local",container="monetizacion"}))) / sum by (pod) (kube_pod_container_resource_limits{namespace="aws-local",container="monetizacion",resource="memory"})')"
    echo "   reinicios (aws-local)          $(prom 'sum(kube_pod_container_status_restarts_total{namespace="aws-local"})')"
  } | tee -a "$BITACORA"
}

# Petición HTTP desde dentro del clúster (el Pod del gateway tiene Python).
# en_cluster METODO URL [JSON]
en_cluster() {
  kubectl exec -n "$NS" deploy/api-gateway -- python -c '
import json, sys, urllib.request
metodo, url = sys.argv[1], sys.argv[2]
cuerpo = sys.argv[3].encode() if len(sys.argv) > 3 else None
peticion = urllib.request.Request(url, data=cuerpo, method=metodo, headers={"Content-Type": "application/json"})
print(urllib.request.urlopen(peticion, timeout=15).read().decode())' "$@"
}

psql_en() { kubectl exec -n "$NS" postgres-0 -- psql -U jobbi -d "jobbi_$1" -tAc "$2"; }

# Carga nominal de fondo. carga_de_fondo <escenario> [VAR=valor ...]
carga_de_fondo() {
  marca "Carga de fondo: k6 '$1' ${*:2}"
  "$RAIZ/scripts/k6-en-cluster.sh" "$@" > "$BITACORA.k6.txt" 2>&1 &
  K6_PID=$!
}

esperar_carga() {
  marca "Esperando a que termine la carga (no interrumpas: después viene la verificación de integridad)…"
  # Muestra el avance de k6 cada 20 s para que se vea que sigue vivo.
  while kill -0 "$K6_PID" 2>/dev/null; do
    sleep 20
    kill -0 "$K6_PID" 2>/dev/null || break
    local avance
    avance=$(grep -oE 'running \([^)]*\)[^,]*, [0-9]+/[0-9]+ VUs, [0-9]+ complete' "$BITACORA.k6.txt" 2>/dev/null | tail -1 || true)
    echo "   · k6 sigue: ${avance:-arrancando…}"
  done
  wait "$K6_PID" || true
  sed -n '/checks\.\.\./,/vus_max/p' "$BITACORA.k6.txt" | tee -a "$BITACORA" || true
  grep -E "✓ prestador|✗ prestador|outbox pendiente|latencia outbox" "$BITACORA.k6.txt" | tee -a "$BITACORA" || true
}

# espera SEGUNDOS MOTIVO — muestra cuánto falta cada 15 s.
espera() {
  marca "… $1 s ($2)"
  local restante=$1
  while [ "$restante" -gt 15 ]; do
    sleep 15; restante=$((restante - 15))
    echo "   · faltan ${restante} s"
  done
  sleep "$restante"
}

cerrar_con_integridad() {
  marca "Verificación de integridad (contrataciones completadas desde $INICIO_UTC UTC; puede tardar hasta 2 min)"
  if "$RAIZ/scripts/caos/verificar-integridad.sh" "$INICIO_UTC" | tee -a "$BITACORA"; then
    marca "RESULTADO: integridad OK"
  else
    marca "RESULTADO: integridad con hallazgos (ver arriba)"
    return 1
  fi
}
