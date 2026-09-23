#!/usr/bin/env bash
# Prueba de humo del backend.
#
# La base arranca vacía y no hay UUID fijos, así que el script primero crea sus
# propios datos por la API (un prestador, un demandante, un oficio y un chat) y
# después recorre un endpoint de cada contexto delimitado a través del gateway.
#
#   ./scripts/smoke-test-backend.sh                  # contra localhost:8080
#   ./scripts/smoke-test-backend.sh http://otra:8080
#
# En Minikube, deja el port-forward corriendo en otra terminal y llama al script
# sin argumentos:
#   kubectl port-forward svc/api-gateway 8080:8080 -n aws-local
set -uo pipefail

BASE="${1:-http://127.0.0.1:8080}"

if ! curl -s --max-time 5 -o /dev/null "$BASE/health"; then
  echo "No se pudo contactar el gateway en $BASE" >&2
  echo >&2
  echo "Si está desplegado en Minikube, abre el túnel en otra terminal:" >&2
  echo "    kubectl port-forward svc/api-gateway 8080:8080 -n aws-local" >&2
  echo >&2
  echo "Si lo corres en local:" >&2
  echo "    ./scripts/run-backend-local.sh" >&2
  exit 1
fi

OK=0
FALLOS=0
# Sufijo único por ejecución: correr el script dos veces no choca con el 409 de
# correo repetido ni ensucia los datos de la corrida anterior.
SUFIJO="$(date +%s).$$.${RANDOM}"

# Extrae un valor anidado del último cuerpo recibido, con una ruta tipo "a.b".
json() {
  python3 -c '
import json, sys
dato = json.load(sys.stdin)
for clave in sys.argv[1].split("."):
    dato = dato[clave]
print(dato)
' "$1" 2>/dev/null
}

probar() {
  local descripcion="$1" ruta="$2"
  local codigo
  codigo=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE$ruta")
  if [ "$codigo" = "200" ]; then
    printf '  \033[32m✓\033[0m %-3s %-46s %s\n' "$codigo" "$descripcion" "$ruta"
    OK=$((OK + 1))
  else
    printf '  \033[31m✗\033[0m %-3s %-46s %s\n' "$codigo" "$descripcion" "$ruta"
    FALLOS=$((FALLOS + 1))
  fi
}

probar_post() {
  local descripcion="$1" ruta="$2" cuerpo="$3" esperado="$4"
  local codigo
  codigo=$(curl -s -o /tmp/jobbi-smoke.json -w '%{http_code}' --max-time 10 -X POST "$BASE$ruta" \
    -H 'Content-Type: application/json' -d "$cuerpo")
  if [ "$codigo" = "$esperado" ]; then
    printf '  \033[32m✓\033[0m %-3s %-46s %s\n' "$codigo" "$descripcion" "POST $ruta"
    OK=$((OK + 1))
  else
    printf '  \033[31m✗\033[0m %-3s %-46s %s (esperado %s)\n' "$codigo" "$descripcion" "POST $ruta" "$esperado"
    FALLOS=$((FALLOS + 1))
  fi
}

echo "=== Smoke test del backend JOBBI contra $BASE ==="
echo
echo "-- Operación --"
probar "salud del gateway"              "/health"
probar "salud de todos los contextos"   "/health/servicios"
probar "catálogo de servicios"          "/api/servicios"

echo
echo "-- Alta de cuentas (la base arranca vacía) --"
probar_post "registro de prestador" "/api/auth/registro" \
  "{\"nombreCompleto\":\"Humo Prestador\",\"correo\":\"humo.prest.$SUFIJO@example.com\",\"telefono\":\"3001112222\",\"numeroDocumento\":\"10101010\",\"rol\":\"Prestador\",\"descripcion\":\"Prueba de humo\",\"tarifaReferencialBase\":85000}" 201
PRESTADOR=$(json "perfilPrestador.id" < /tmp/jobbi-smoke.json)

probar_post "registro de demandante" "/api/auth/registro" \
  "{\"nombreCompleto\":\"Humo Demandante\",\"correo\":\"humo.dem.$SUFIJO@example.com\",\"telefono\":\"3003334444\",\"numeroDocumento\":\"20202020\",\"rol\":\"Demandante\"}" 201
DEMANDANTE=$(json "perfilDemandante.id" < /tmp/jobbi-smoke.json)
USUARIO_DEM=$(json "usuario.id" < /tmp/jobbi-smoke.json)

probar_post "login con el correo recién creado" "/api/auth/login" \
  "{\"correo\":\"humo.prest.$SUFIJO@example.com\"}" 200
probar_post "login con correo inexistente" "/api/auth/login" \
  "{\"correo\":\"nadie.$SUFIJO@example.com\"}" 404
probar_post "registro con correo duplicado" "/api/auth/registro" \
  "{\"nombreCompleto\":\"Humo Repetido\",\"correo\":\"humo.dem.$SUFIJO@example.com\",\"telefono\":\"3003334444\",\"numeroDocumento\":\"20202020\",\"rol\":\"Demandante\"}" 409

echo
echo "-- Catálogo construido desde el registro --"
probar_post "alta de oficio (crea la categoría)" "/api/mercado/catalogo/oficio" \
  "{\"categoriaNombre\":\"Hogar\",\"oficioNombre\":\"Plomería $SUFIJO\"}" 201
OFICIO=$(json "oficio.id" < /tmp/jobbi-smoke.json)
probar_post "alta de la oferta del prestador" "/api/mercado/prestador-oficios" \
  "{\"prestadorId\":\"$PRESTADOR\",\"oficioId\":\"$OFICIO\",\"tarifaReferencial\":85000,\"anosExperiencia\":8}" 201

echo
echo "-- Apertura del chat (Contactar) --"
probar_post "contacto + conversación en una llamada" "/api/bff/contactar" \
  "{\"demandanteId\":\"$DEMANDANTE\",\"prestadorId\":\"$PRESTADOR\"}" 201
CONVERSACION=$(json "conversacion.id" < /tmp/jobbi-smoke.json)
probar_post "idempotencia: contactar otra vez" "/api/bff/contactar" \
  "{\"demandanteId\":\"$DEMANDANTE\",\"prestadorId\":\"$PRESTADOR\"}" 201
probar_post "enviar un mensaje" "/api/comunicacion/mensajes" \
  "{\"conversacionId\":\"$CONVERSACION\",\"remitenteId\":\"$USUARIO_DEM\",\"contenido\":\"Mensaje de prueba de humo.\"}" 201
probar "bandeja del demandante"          "/api/bff/mensajes?demandanteId=$DEMANDANTE"
probar "bandeja del prestador"           "/api/bff/mensajes?prestadorId=$PRESTADOR"
probar "mensajes de la conversación"     "/api/comunicacion/conversaciones/$CONVERSACION/mensajes"

echo
echo "-- Identidad y Perfiles --"
probar "listar usuarios"                "/api/identidad/usuarios"
probar "prestadores verificados"        "/api/identidad/prestadores?verificado=true"
probar "detalle de prestador"           "/api/identidad/prestadores/$PRESTADOR"

echo
echo "-- Mercado de Oficios --"
probar "categorías"                     "/api/mercado/categorias"
probar "oficios"                        "/api/mercado/oficios"
probar "oficios de un prestador"        "/api/mercado/prestadores/$PRESTADOR/oficios"
probar "contactos del demandante"       "/api/mercado/contactos?demandanteId=$DEMANDANTE"
probar "resumen del catálogo"           "/api/mercado/resumen/catalogo"

echo
echo "-- Contrataciones --"
probar "listar contrataciones"          "/api/contrataciones/contrataciones"
probar "resumen del prestador"          "/api/contrataciones/resumen?prestadorId=$PRESTADOR"
probar "enumeraciones del flujo"        "/api/contrataciones/enums"

echo
echo "-- Comunicación --"
probar "conversaciones"                 "/api/comunicacion/conversaciones"
probar "notificaciones no leídas"       "/api/comunicacion/notificaciones?leida=false"

echo
echo "-- Confianza y Verificación --"
probar "verificaciones aprobadas"       "/api/confianza/verificaciones?estado=APROBADA"
probar "resumen de reseñas"             "/api/confianza/resenas/resumen?receptorId=$PRESTADOR"
probar "cola de moderación"             "/api/confianza/moderacion/cola"

echo
echo "-- Monetización --"
probar "pagos"                          "/api/monetizacion/pagos"
probar "billeteras"                     "/api/monetizacion/billeteras"
probar "resumen financiero"             "/api/monetizacion/resumen/prestador/$PRESTADOR"
probar "cobros del worker SQS"          "/api/monetizacion/cobros"

echo
echo "-- Soporte, Adquisición y Protección --"
probar "incidentes abiertos"            "/api/soporte/incidentes?abiertos=true"
probar "resumen de incidentes"          "/api/soporte/incidentes/resumen"
probar "aliados de distribución"        "/api/adquisicion/aliados-distribucion"
probar "planes de protección"           "/api/proteccion/planes-proteccion"

echo
echo "-- Composición BFF --"
probar "ficha 360° del prestador"       "/api/bff/prestadores/$PRESTADOR"
probar "inicio del demandante"          "/api/bff/demandantes/$DEMANDANTE/inicio"
probar "catálogo de búsqueda"           "/api/bff/catalogo"
probar "métricas de admin"              "/api/bff/admin/metricas"

echo
echo "=== Resultado: $OK correctos, $FALLOS fallidos ==="
[ "$FALLOS" -eq 0 ]
