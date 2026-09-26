#!/usr/bin/env bash
# ESTRÉS · Punto de quiebre: rampa 75 → 125 → 250 → 350 TPS y vuelta a 12 TPS.
#
#   ./scripts/caos/estres-punto-de-quiebre.sh
#
# Objetivo: encontrar el primer recurso que se satura y documentar la
# recuperación. Candidatos, en el orden en que se espera que aparezcan:
#   - CPU de los Pods (límites de 300 m): % del límite en el panel de infraestructura.
#   - Pool de SQLAlchemy (5 + 10 por servicio) y max_connections de PostgreSQL
#     (100 frente a 150 posibles): pool en uso, conexiones PostgreSQL, 503.
#   - La propia tasa de llegada: k6 descarta iteraciones (dropped_iterations)
#     cuando el sistema ya no alcanza a responder.
#
# Toma una foto de las métricas al final de cada escalón (~13 min en total).
set -euo pipefail
source "$(dirname "$0")/comun.sh"

iniciar_bitacora estres-punto-de-quiebre
foto "REPOSO"
carga_de_fondo quiebre
for escalon in "75 TPS" "125 TPS" "250 TPS" "350 TPS"; do
  espera 150 "escalón $escalon (30 s de subida + 2 min sostenido)"
  foto "FIN DEL ESCALÓN $escalon"
done
espera 150 "vuelta a 12 TPS"
foto "RECUPERACIÓN (12 TPS)"

esperar_carga
grep -E "dropped_iterations|http_req_failed|http_reqs" "$BITACORA.k6.txt" | tee -a "$BITACORA" || true
cerrar_con_integridad
