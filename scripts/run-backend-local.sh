#!/usr/bin/env bash
# Levanta todo el backend en la máquina local, sin Kubernetes.
#
# Útil para desarrollar e iterar rápido. Cada contexto corre en su propio
# proceso y puerto, igual que en el clúster, así que el gateway funciona con la
# misma topología lógica. Ctrl+C detiene todo.
#
# Pub/Sub: levanta LocalStack (SNS + SQS) en Docker y el cobro de comisiones
# viaja como evento, igual que en el clúster. Docker es obligatorio: el cobro
# solo existe por eventos (ADR-002), no hay un modo síncrono sin broker.
#
#   ./scripts/run-backend-local.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT_DIR/.venv"
LOGS="$ROOT_DIR/.logs"

if [ ! -d "$VENV" ]; then
  echo "=== Creando entorno virtual en .venv ==="
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$ROOT_DIR/services/requirements.txt"
fi

mkdir -p "$LOGS"
cd "$ROOT_DIR/services"

LOCALSTACK="jobbi-localstack"
if ! docker info >/dev/null 2>&1; then
  echo "!!! Docker no está disponible: sin LocalStack nadie entrega los eventos y las"
  echo "    comisiones no se cobrarían. Inicia Docker y vuelve a intentarlo."
  exit 1
fi

echo "=== Levantando LocalStack (SNS + SQS) en Docker ==="
docker rm -f "$LOCALSTACK" >/dev/null 2>&1 || true
# El script de aprovisionamiento se monta como init hook: LocalStack crea los
# temas, las colas y las DLQ al arrancar. Se copia a .logs/ porque montar un
# archivo que luego se edita deja al contenedor viendo una versión a medias.
cp "$ROOT_DIR/scripts/init-aws-local.sh" "$LOGS/localstack-init.sh"
docker run -d --name "$LOCALSTACK" -p 4566:4566 \
  -e SERVICES=sqs,sns -e EAGER_SERVICE_LOADING=1 \
  -v "$LOGS/localstack-init.sh:/etc/localstack/init/ready.d/01-pubsub.sh:ro" \
  localstack/localstack:3.0.0 >/dev/null
printf '  Esperando el aprovisionamiento'
for _ in $(seq 1 60); do
  if curl -s localhost:4566/_localstack/init/ready 2>/dev/null | grep -q '"completed": true'; then break; fi
  printf '.'; sleep 2
done
echo " listo"

export SNS_ENABLED=true SQS_ENABLED=true
export AWS_ENDPOINT_URL="http://127.0.0.1:4566"
# Confianza consulta al aliado de verificación simulado (Adapter + Circuit Breaker).
export VERIFICACION_URL="http://127.0.0.1:8010"

# Sin DATABASE_URL, cada contexto usa su propio archivo SQLite bajo .datos/.
# Así el backend completo corre sin instalar PostgreSQL, y la separación entre
# contextos se mantiene: un archivo por contexto, nunca uno compartido.
export SQLITE_DIR="${SQLITE_DIR:-$ROOT_DIR/.datos}"

# El gateway resuelve cada contexto en localhost en vez del DNS del clúster.
export SERVICIO_IDENTIDAD_URL="http://127.0.0.1:8001"
export SERVICIO_MERCADO_URL="http://127.0.0.1:8002"
export SERVICIO_CONTRATACIONES_URL="http://127.0.0.1:8003"
export SERVICIO_COMUNICACION_URL="http://127.0.0.1:8004"
export SERVICIO_CONFIANZA_URL="http://127.0.0.1:8005"
export SERVICIO_MONETIZACION_URL="http://127.0.0.1:8000"
export SERVICIO_SOPORTE_URL="http://127.0.0.1:8006"
export SERVICIO_ADQUISICION_URL="http://127.0.0.1:8007"
export SERVICIO_PROTECCION_URL="http://127.0.0.1:8008"

PIDS=()
levantar() {
  local modulo="$1" puerto="$2"
  "$VENV/bin/uvicorn" "$modulo.main:app" --host 127.0.0.1 --port "$puerto" --no-access-log \
    >"$LOGS/$modulo.log" 2>&1 &
  PIDS+=($!)
  printf '  %-16s http://127.0.0.1:%s/docs\n' "$modulo" "$puerto"
}

cleanup() {
  echo
  echo "=== Deteniendo servicios ==="
  kill "${PIDS[@]}" 2>/dev/null || true
  wait 2>/dev/null || true
  docker rm -f "$LOCALSTACK" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "=== Levantando microservicios de dominio ==="
levantar monetizacion 8000
levantar identidad 8001
levantar mercado 8002
levantar contrataciones 8003
levantar comunicacion 8004
levantar confianza 8005
levantar soporte 8006
levantar adquisicion 8007
levantar proteccion 8008
levantar verificacion_externa 8010   # aliado simulado, no es un contexto de dominio

echo "=== Levantando API Gateway ==="
levantar gateway 8080

echo
echo "Gateway:   http://127.0.0.1:8080/docs"
echo "Estado:    curl -s http://127.0.0.1:8080/health/servicios"
echo "Logs en:   $LOGS/"
echo "Datos en:  $SQLITE_DIR/  (un archivo SQLite por contexto)"
echo "Pub/Sub:   LocalStack en http://localhost:4566 · estado en /api/bff/pubsub/estado"
echo "Ctrl+C para detener todo."
wait
