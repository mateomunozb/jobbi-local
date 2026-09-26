#!/usr/bin/env bash
# Crea las cuentas de demostración: 2 prestadores (con su oficio y tarifa
# publicados, para que aparezcan en la búsqueda) y 2 demandantes.
#
#   ./scripts/reset-datos.sh && ./scripts/sembrar-demo.sh    # empezar de cero
#
# Todo pasa por el API Gateway, igual que desde la aplicación: nada se escribe
# directo en la base. Es idempotente: si una cuenta ya existe, no la duplica.
# No necesita port-forward (se ejecuta dentro del Pod del gateway).
#
# El login es solo con el correo (ver la nota de alcance en services/identidad/auth.py).
set -euo pipefail

NS="aws-local"
kubectl rollout status deploy/api-gateway -n "$NS" --timeout=120s >/dev/null

kubectl exec -i -n "$NS" deploy/api-gateway -- python - <<'PY'
import json
import urllib.error
import urllib.request

BASE = "http://localhost:8080/api"


def post(ruta, cuerpo):
    peticion = urllib.request.Request(BASE + ruta, data=json.dumps(cuerpo).encode(), method="POST",
                                      headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(peticion, timeout=20) as respuesta:
            return respuesta.status, json.loads(respuesta.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def cuenta(datos):
    estado, sesion = post("/auth/registro", datos)
    if estado == 409:  # ya existía: se reutiliza
        estado, sesion = post("/auth/login", {"correo": datos["correo"]})
    if estado not in (200, 201):
        raise SystemExit(f"No se pudo crear {datos['correo']}: {estado} {sesion}")
    return sesion


PRESTADORES = [
    {"nombreCompleto": "Prestador 1", "correo": "prestador1@demo.jobbi.co", "telefono": "3001112233",
     "numeroDocumento": "1017000001", "municipio": "Medellín", "barrio": "Laureles",
     "descripcion": "Plomería: reparaciones e instalaciones del hogar.",
     "tarifaReferencialBase": 80000, "_categoria": "Hogar", "_oficio": "Plomería", "_anos": 8},
    {"nombreCompleto": "Prestador 2", "correo": "prestador2@demo.jobbi.co", "telefono": "3004445566",
     "numeroDocumento": "1017000002", "municipio": "Envigado", "barrio": "El Portal",
     "descripcion": "Electricidad: redes domésticas, tomas y tableros.",
     "tarifaReferencialBase": 95000, "_categoria": "Hogar", "_oficio": "Electricidad", "_anos": 6},
]
DEMANDANTES = [
    {"nombreCompleto": "Demandante 1", "correo": "demandante1@demo.jobbi.co", "telefono": "3007778899",
     "numeroDocumento": "1017000003", "municipio": "Medellín", "barrio": "Belén"},
    {"nombreCompleto": "Demandante 2", "correo": "demandante2@demo.jobbi.co", "telefono": "3009990011",
     # El aliado simulado rechaza ~20 % de los documentos (siempre los mismos):
     # 1017000004 sale con antecedentes y el demandante no podría contratar (RN-01).
     "numeroDocumento": "1017000005", "municipio": "Sabaneta", "barrio": "Centro"},
]

print("Prestadores:")
for p in PRESTADORES:
    extra = {k: p.pop(k) for k in ("_categoria", "_oficio", "_anos")}
    sesion = cuenta({**p, "rol": "Prestador"})
    prestador_id = sesion["perfilPrestador"]["id"]
    estado, alta = post("/mercado/catalogo/oficio", {"categoriaNombre": extra["_categoria"],
                                                     "oficioNombre": extra["_oficio"]})
    if estado not in (200, 201):
        raise SystemExit(f"No se pudo crear el oficio {extra['_oficio']}: {estado} {alta}")
    estado, oferta = post("/mercado/prestador-oficios", {
        "prestadorId": prestador_id, "oficioId": alta["oficio"]["id"],
        "tarifaReferencial": p["tarifaReferencialBase"], "anosExperiencia": extra["_anos"]})
    if estado not in (200, 201, 409):
        raise SystemExit(f"No se pudo publicar la oferta de {p['correo']}: {estado} {oferta}")
    estado = sesion["perfilPrestador"]["estadoVerificacionActual"]
    print(f"  ✓ {p['nombreCompleto']:<16} {p['correo']:<34} {extra['_oficio']} · ${p['tarifaReferencialBase']:,} · verificación {estado}")

print("Demandantes:")
for d in DEMANDANTES:
    cuenta({**d, "rol": "Demandante"})
    print(f"  ✓ {d['nombreCompleto']:<16} {d['correo']}")
PY

cat <<'FIN'

=== Cuentas de demostración listas ===
Entra con el correo (sin contraseña) en http://localhost:3000:
  kubectl port-forward svc/jobbi-frontend 3000:3000 -n aws-local
  kubectl port-forward svc/api-gateway 8080:8080 -n aws-local
FIN
