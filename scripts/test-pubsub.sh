#!/usr/bin/env bash
# Prueba funcional del Pub/Sub, de punta a punta y a través del API Gateway.
#
# Crea un prestador y un demandante, recorre el flujo real (contacto → tarifa →
# check-in → check-out) y sigue el evento CONTRATACION_COMPLETADA por cada etapa
# hasta que Monetización cobra la comisión:
#
#   EN_OUTBOX  →  PUBLICADO (SNS → SQS)  →  COBRADO (worker → billetera)
#
#   ./scripts/test-pubsub.sh                       # gateway en localhost:8080
#   ./scripts/test-pubsub.sh --consumidor-caido    # (Minikube) apaga Monetización
#                                                  # antes del check-out
#
# Con --consumidor-caido se demuestra el desacoplamiento: el check-out responde
# igual, el evento espera en la cola SQS y se cobra cuando Monetización vuelve.
#
# Crea datos propios: no lo corras justo antes de una demostración en vivo
# (o deja el sistema limpio después con ./scripts/reset-datos.sh).
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
NS="aws-local"
CONSUMIDOR_CAIDO=0
[ "${1:-}" = "--consumidor-caido" ] && CONSUMIDOR_CAIDO=1

command -v jq >/dev/null || { echo "Falta jq (brew install jq)"; exit 1; }

SUFIJO="$(date +%s)$RANDOM"
ok()    { printf '  \033[32m✓\033[0m %s\n' "$1"; }
falla() { printf '  \033[31m✗ %s\033[0m\n' "$1"; exit 1; }
post()  { curl -sf -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
get()   { curl -sf "$BASE$1"; }

echo "=== 0 · Estado del backend ($BASE) ==="
get /health/servicios | jq -e '.disponibles == .totalServicios' >/dev/null \
  || falla "Hay servicios caídos: revisa $BASE/health/servicios"
MODO=$(get /api/bff/pubsub/estado | jq -r .modo)
[ "$MODO" = "EVENTOS" ] || falla "El gateway está en modo $MODO: arranca con Pub/Sub (LocalStack)"
ok "9 contextos arriba · cobro vía eventos"

echo "=== 1 · Datos de prueba ==="
PRESTADOR=$(post /api/auth/registro "{\"nombreCompleto\":\"Prestador PubSub $SUFIJO\",\"correo\":\"prestador.$SUFIJO@pubsub.jobbi.co\",\"telefono\":\"3001234567\",\"numeroDocumento\":\"P$SUFIJO\",\"rol\":\"Prestador\",\"descripcion\":\"Prueba Pub/Sub\",\"tarifaReferencialBase\":80000}" | jq -r .perfilPrestador.id)
OFICIO=$(post /api/mercado/catalogo/oficio '{"categoriaNombre":"Hogar","oficioNombre":"Plomería"}' | jq -r .oficio.id)
post /api/mercado/prestador-oficios "{\"prestadorId\":\"$PRESTADOR\",\"oficioId\":\"$OFICIO\",\"tarifaReferencial\":80000,\"anosExperiencia\":5}" >/dev/null
DEMANDANTE=$(post /api/auth/registro "{\"nombreCompleto\":\"Demandante PubSub $SUFIJO\",\"correo\":\"demandante.$SUFIJO@pubsub.jobbi.co\",\"telefono\":\"3007654321\",\"numeroDocumento\":\"D$SUFIJO\",\"rol\":\"Demandante\"}" | jq -r .perfilDemandante.id)
ok "prestador $PRESTADOR · demandante $DEMANDANTE"

SALDO_ANTES=$(get "/api/monetizacion/resumen/prestador/$PRESTADOR" | jq '.billetera.saldoPendiente // 0')

echo "=== 2 · Contacto, tarifa y servicio ==="
CHAT=$(post /api/bff/contactar "{\"demandanteId\":\"$DEMANDANTE\",\"prestadorId\":\"$PRESTADOR\"}")
CONTACTO=$(jq -r .contacto.id <<<"$CHAT"); CONVERSACION=$(jq -r .conversacion.id <<<"$CHAT")
ACUERDO=$(post /api/bff/acuerdos "{\"contactoId\":\"$CONTACTO\",\"conversacionId\":\"$CONVERSACION\",\"demandanteId\":\"$DEMANDANTE\",\"prestadorId\":\"$PRESTADOR\",\"oficioId\":\"$OFICIO\",\"valorPropuesto\":120000,\"medioPago\":\"EFECTIVO\",\"propuestoPor\":\"DEMANDANTE\"}" | jq -r .acuerdo.id)
CONTRATACION=$(post "/api/bff/acuerdos/$ACUERDO/aceptar" '{"rol":"PRESTADOR"}')
ID=$(jq -r .contratacion.id <<<"$CONTRATACION"); COMISION=$(jq .contratacion.montoComision <<<"$CONTRATACION")
post "/api/bff/contrataciones/$ID/check-in" '{}' >/dev/null
ok "contratación $ID · comisión congelada $COMISION COP"

if [ "$CONSUMIDOR_CAIDO" = "1" ]; then
  echo "=== 2b · Apagando Monetización (consumidor) ==="
  kubectl scale deployment/servicio-monetizacion -n "$NS" --replicas=0 >/dev/null
  kubectl wait --for=delete pod -l app=servicio-monetizacion -n "$NS" --timeout=60s >/dev/null 2>&1 || true
  ok "servicio-monetizacion en 0 réplicas"
fi

echo "=== 3 · Check-out (el productor publica el evento) ==="
INICIO=$(python3 -c 'import time; print(time.time())')
CIERRE=$(post "/api/bff/contrataciones/$ID/check-out" '{}')
[ "$(jq -r .contratacion.estado <<<"$CIERRE")" = "COMPLETADA" ] || falla "La contratación no quedó COMPLETADA"
[ "$(jq -r .cobroAsincrono <<<"$CIERRE")" = "true" ] || falla "El check-out cobró de forma síncrona"
ok "check-out respondió sin esperar a Monetización (cobro: $(jq -c .cobro <<<"$CIERRE"))"

seguir() {  # $1 = etapa esperada, $2 = segundos máximos
  local etapa="" anterior=""
  for _ in $(seq 1 $(( $2 * 4 ))); do
    etapa=$(get "/api/bff/contrataciones/$ID/cobro" | jq -r .etapa || echo "?")
    if [ "$etapa" != "$anterior" ]; then
      printf '    %6.2fs  %s\n' "$(python3 -c "import time; print(time.time() - $INICIO)")" "$etapa"
      anterior="$etapa"
    fi
    [ "$etapa" = "$1" ] && return 0
    sleep 0.25
  done
  return 1
}

echo "=== 4 · Siguiendo el evento ==="
if [ "$CONSUMIDOR_CAIDO" = "1" ]; then
  seguir PUBLICADO 20 || falla "El evento no llegó a SNS/SQS"
  sleep 3
  [ "$(get "/api/bff/contrataciones/$ID/cobro" | jq -r .etapa)" = "PUBLICADO" ] \
    || falla "Se cobró con el consumidor apagado (?)"
  ok "el evento espera en la cola mientras Monetización está caída"
  echo "=== 4b · Encendiendo Monetización ==="
  kubectl scale deployment/servicio-monetizacion -n "$NS" --replicas=1 >/dev/null
  kubectl rollout status deployment/servicio-monetizacion -n "$NS" --timeout=120s >/dev/null
  seguir COBRADO 60 || falla "El evento no se cobró al volver el consumidor"
else
  seguir COBRADO 30 || falla "La comisión no se cobró en 30 s: revisa /api/bff/pubsub/estado"
fi
ok "comisión cobrada por el worker SQS"

echo "=== 5 · Verificaciones ==="
SALDO_DESPUES=$(get "/api/monetizacion/resumen/prestador/$PRESTADOR" | jq '.billetera.saldoPendiente')
python3 -c "import sys; sys.exit(0 if abs($SALDO_DESPUES - $SALDO_ANTES - $COMISION) < 0.01 else 1)" \
  || falla "Saldo esperado $SALDO_ANTES + $COMISION, obtenido $SALDO_DESPUES"
ok "billetera: $SALDO_ANTES → $SALDO_DESPUES COP"

# Reintentar el check-out no emite otro evento ni cobra dos veces.
post "/api/bff/contrataciones/$ID/check-out" '{}' >/dev/null; sleep 2
EVENTOS=$(get "/api/contrataciones/outbox?agregadoId=$ID" | jq .total)
SALDO_FINAL=$(get "/api/monetizacion/resumen/prestador/$PRESTADOR" | jq '.billetera.saldoPendiente')
[ "$EVENTOS" = "1" ] && [ "$SALDO_FINAL" = "$SALDO_DESPUES" ] || falla "El check-out repetido duplicó el cobro"
ok "check-out repetido: 1 evento, sin doble cobro"

echo "=== Estado del Pub/Sub ==="
get /api/bff/pubsub/estado | jq '{
  outbox: {pendientes: .productor.pendientes, publicados: .productor.publicados,
           latenciaPublicacionMs: .productor.latenciaPublicacionMs.promedio},
  cola: .consumidor.colas.principal, dlq: .consumidor.colas.dlq,
  consumidor: .consumidor.porResultado,
  latenciaExtremoAExtremoMs: .consumidor.latenciaExtremoAExtremoMs}'
echo
echo "=== Pub/Sub OK ==="
