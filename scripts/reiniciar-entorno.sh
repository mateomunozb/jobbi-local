#!/usr/bin/env bash
# Deja el entorno en un estado conocido antes de cada prueba de carga o de fallo.
#
#   ./scripts/reiniciar-entorno.sh            # reinicia los servicios; conserva los datos
#   ./scripts/reiniciar-entorno.sh --datos    # además borra TODO y siembra las 4 cuentas demo
#   ./scripts/reiniciar-entorno.sh --todo     # incluye gateway y frontend (corta los port-forward)
#   ./scripts/reiniciar-entorno.sh --metricas # además vacía Prometheus: Grafana arranca sin histórico
#
# Las opciones se combinan. Para que la siguiente prueba arranque con TODO en
# cero (gráficas y contadores de negocio): --datos --metricas
#
# Qué hace, en orden:
#   1. Retira cualquier fallo que haya quedado inyectado: PostgreSQL con 1 réplica
#      y el aliado de verificación respondiendo normal.
#   2. Detiene las pruebas k6 que sigan corriendo en el clúster.
#   3. Espera a que no queden eventos en vuelo (outbox y colas vacías) para no
#      perder cobros al reiniciar el broker.
#   4. Reinicia LocalStack (colas limpias) y todos los servicios de dominio: los
#      contadores en memoria, los hilos consumidores y el Circuit Breaker vuelven
#      a su estado inicial.
#   5. Comprueba que los 9 contextos respondan y que ambos consumidores estén
#      conectados a su cola.
#
# Prometheus y Grafana NO se reinician. Sin --metricas conservan la historia
# (sirve para comparar una prueba con la anterior); con --metricas se borran
# todas las series guardadas y las gráficas arrancan vacías desde ese momento.
# Los contadores de negocio (billeteras bloqueadas, recaudo, saldo) salen de la
# base de datos: para ponerlos en cero hace falta además --datos.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS="aws-local"
DATOS=false; TODO=false; METRICAS=false
for opcion in "$@"; do
  case "$opcion" in
    --datos) DATOS=true ;;
    --todo) TODO=true ;;
    --metricas) METRICAS=true ;;
    *) echo "Opción desconocida '$opcion' (usa --datos, --todo y/o --metricas)"; exit 1 ;;
  esac
done
paso() { echo; echo "=== $* ==="; }
psql_en() { kubectl exec -n "$NS" postgres-0 -- psql -U jobbi -d "jobbi_$1" -tAc "$2"; }

paso "1. Retirando fallos inyectados"
kubectl scale statefulset/postgres -n "$NS" --replicas=1 >/dev/null
kubectl wait --for=condition=Ready pod/postgres-0 -n "$NS" --timeout=300s >/dev/null
echo "  ✓ PostgreSQL arriba"
kubectl scale deploy/verificacion-externa -n "$NS" --replicas=1 >/dev/null
kubectl rollout status deploy/verificacion-externa -n "$NS" --timeout=120s >/dev/null
kubectl exec -n "$NS" deploy/verificacion-externa -- python -c \
  "import urllib.request as u; u.urlopen(u.Request('http://localhost:8010/_caos', method='DELETE'), timeout=5)" \
  >/dev/null 2>&1 && echo "  ✓ aliado de verificación sin fallos" || echo "  - aliado no disponible (se reinicia abajo)"

paso "2. Deteniendo pruebas k6 en curso"
kubectl delete jobs -n pruebas -l app=k6 --ignore-not-found --wait=true 2>/dev/null | sed 's/^/  /' || true

if [ "$DATOS" = true ]; then
  paso "3-4. Borrando todos los datos y sembrando las cuentas demo"
  "$RAIZ/scripts/reset-datos.sh"
else
  paso "3. Esperando a que no queden eventos en vuelo (máx. 90 s)"
  for _ in $(seq 1 30); do
    pendientes=$(( $(psql_en contrataciones "SELECT count(*) FROM outbox_eventos WHERE estado='PENDIENTE'") \
                 + $(psql_en monetizacion "SELECT count(*) FROM outbox_monetizacion WHERE estado='PENDIENTE'") ))
    en_cola=0
    for cola in monetizacion-events-queue comunicacion-events-queue; do
      n=$(kubectl exec -n "$NS" deploy/localstack -- awslocal sqs get-queue-attributes \
            --queue-url "http://localhost:4566/000000000000/$cola" \
            --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
            --query 'sum([to_number(Attributes.ApproximateNumberOfMessages), to_number(Attributes.ApproximateNumberOfMessagesNotVisible)])' \
            --output text 2>/dev/null || echo 0)
      en_cola=$(( en_cola + ${n%.*} ))
    done
    [ "$pendientes" -eq 0 ] && [ "$en_cola" -eq 0 ] && break
    sleep 3
  done
  echo "  outbox pendientes: $pendientes · mensajes en colas: $en_cola"
  [ "$pendientes" -eq 0 ] && [ "$en_cola" -eq 0 ] || echo "  !!! quedan eventos en vuelo: reiniciar el broker podría perderlos"

  paso "4. Reiniciando LocalStack y los servicios"
  kubectl rollout restart deploy/localstack -n "$NS" >/dev/null
  kubectl rollout status deploy/localstack -n "$NS" --timeout=180s | sed 's/^/  /'
  DEPLOYS="identidad mercado contrataciones comunicacion confianza soporte adquisicion proteccion servicio-monetizacion"
  for d in $DEPLOYS; do kubectl rollout restart "deploy/$d" -n "$NS" >/dev/null; done
  for d in $DEPLOYS; do kubectl rollout status "deploy/$d" -n "$NS" --timeout=180s | sed 's/^/  /'; done
fi

# El simulador del aliado guarda el fallo en memoria: se reinicia siempre.
kubectl rollout restart deploy/verificacion-externa -n "$NS" >/dev/null
kubectl rollout status deploy/verificacion-externa -n "$NS" --timeout=120s | sed 's/^/  /'
if [ "$TODO" = true ]; then
  echo "  (--todo: se reinician gateway y frontend; vuelve a abrir los port-forward)"
  kubectl rollout restart deploy/api-gateway deploy/jobbi-frontend -n "$NS" >/dev/null
  kubectl rollout status deploy/api-gateway -n "$NS" --timeout=180s | sed 's/^/  /'
  kubectl rollout status deploy/jobbi-frontend -n "$NS" --timeout=180s | sed 's/^/  /'
fi

paso "5. Comprobando el estado"
for _ in $(seq 1 30); do
  estado=$(kubectl exec -n "$NS" deploy/api-gateway -- python -c '
import json, urllib.request as u
salud = json.load(u.urlopen("http://localhost:8080/health/servicios", timeout=10))
pubsub = json.load(u.urlopen("http://localhost:8080/api/bff/pubsub/estado", timeout=10))
avisos = json.load(u.urlopen("http://localhost:8080/api/comunicacion/eventos/estado-worker", timeout=10))
print(salud["disponibles"], salud["totalServicios"], bool((pubsub.get("consumidor") or {}).get("conectado")), bool(avisos.get("conectado")))
' 2>/dev/null || echo "0 9 False False")
  read -r disponibles total monet comun <<<"$estado"
  [ "$disponibles" = "$total" ] && [ "$monet" = "True" ] && [ "$comun" = "True" ] && break
  sleep 3
done
echo "  servicios disponibles: $disponibles/$total · consumidor Monetización conectado: $monet · consumidor Comunicación conectado: $comun"

if [ "$DATOS" = true ]; then
  "$RAIZ/scripts/sembrar-demo.sh"
fi

if [ "$METRICAS" = true ]; then
  paso "6. Vaciando el histórico de Prometheus"
  # Borra todas las series y compacta. Prometheus sigue recolectando: las
  # gráficas vuelven a llenarse desde este instante.
  todas='%7B__name__%3D~%22.%2B%22%7D'   # {__name__=~".+"}
  kubectl exec -n monitoring deploy/prometheus -- wget -qO- --post-data='' \
    "http://localhost:9090/api/v1/admin/tsdb/delete_series?match%5B%5D=$todas" >/dev/null
  kubectl exec -n monitoring deploy/prometheus -- wget -qO- --post-data='' \
    "http://localhost:9090/api/v1/admin/tsdb/clean_tombstones" >/dev/null
  echo "  ✓ histórico borrado: en Grafana, recarga el tablero"
fi

if [ "$disponibles" = "$total" ] && [ "$monet" = "True" ] && [ "$comun" = "True" ]; then
  echo; echo "=== Entorno listo para la siguiente prueba ==="
else
  echo; echo "!!! El entorno no quedó completo: revisa 'kubectl get pods -n $NS'"; exit 1
fi
