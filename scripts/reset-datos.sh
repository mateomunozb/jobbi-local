#!/usr/bin/env bash
# Deja el sistema sin un solo dato, como recién instalado.
#
# Vacía las nueve bases (una por contexto) y reinicia los servicios para que
# cada uno vuelva a crear su esquema. No siembra nada: lo que exista después
# será lo que se registre durante la demostración.
#
#   ./scripts/reset-datos.sh            # sobre el clúster (Minikube)
#   ./scripts/reset-datos.sh --local    # sobre los archivos SQLite de .datos/
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS="aws-local"
CONTEXTOS="identidad mercado contrataciones comunicacion confianza monetizacion soporte adquisicion proteccion"

if [ "${1:-}" = "--local" ]; then
  DATOS="${SQLITE_DIR:-$ROOT_DIR/.datos}"
  echo "=== Borrando las bases locales de $DATOS ==="
  rm -f "$DATOS"/*.db
  echo "Listo. Al arrancar ./scripts/run-backend-local.sh cada contexto recreará su esquema vacío."
  exit 0
fi

echo "=== Vaciando las nueve bases en el clúster ==="
# DROP SCHEMA borra las tablas con sus datos; cada servicio las vuelve a crear
# al arrancar, así que también sirve cuando el modelo ganó una tabla nueva.
for contexto in $CONTEXTOS; do
  kubectl exec -n "$NS" statefulset/postgres -- \
    psql -U jobbi -d "jobbi_${contexto}" -q \
      -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION jobbi;" \
    && echo "  ✓ jobbi_${contexto}"
done

echo
echo "=== Reiniciando los servicios para que recreen su esquema ==="
DEPLOYS="identidad mercado contrataciones comunicacion confianza soporte adquisicion proteccion servicio-monetizacion"
for deploy in $DEPLOYS; do kubectl rollout restart "deployment/$deploy" -n "$NS" >/dev/null; done
for deploy in $DEPLOYS; do kubectl rollout status "deployment/$deploy" -n "$NS" --timeout=180s | tail -1; done

cat <<'FIN'

=== Sistema vacío ===

No hay usuarios, ni catálogo, ni contrataciones. Crea las cuentas desde la
aplicación: primero el prestador (declara su oficio y su tarifa) y luego el
demandante, que ya podrá encontrarlo.

Cuidado con el smoke test: crea sus propias cuentas y oficios para probar los
endpoints, así que no lo corras antes de una demostración en vivo.
FIN
