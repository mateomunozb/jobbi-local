#!/usr/bin/env bash
# FALLO 1 · Red / dependencia externa: el aliado de verificación se degrada.
#
#   ./scripts/caos/fallo1-dependencia-externa.sh
#
# El aliado se consulta al REGISTRAR un prestador (y al renovar su veredicto
# cada 30 días). Durante la prueba eso ocurre de verdad: k6 registra un
# prestador nuevo cada pocos segundos para reemplazar a los que llegan al
# umbral de bloqueo (RN-04).
#
# Hipótesis: con el aliado tardando 8 s (y luego respondiendo 504), el Circuit
# Breaker de Confianza se abre tras 3 fallos; mientras está abierto, el registro
# responde al instante y el prestador nuevo queda PENDIENTE (no aparece ni
# trabaja). Los prestadores ya aprobados, los acuerdos, los check-out y los
# cobros no se enteran. Al retirar el fallo el circuito se cierra solo y el
# reverificador de fondo resuelve a los PENDIENTE (APROBADA o RECHAZADA).
#
# Línea de tiempo (~10 min):
#   0:00  carga nominal (12 TPS)          → foto ANTES
#   1:00  aliado con 8 000 ms de retardo  → fotos DURANTE-LATENCIA (2 min)
#   3:00  aliado respondiendo 504         → foto DURANTE-504 (1,5 min)
#   4:30  aliado APAGADO (0 réplicas)     → foto DURANTE-APAGADO (1 min)
#   5:30  aliado de vuelta                → espera > CB_ESPERA_SEGUNDOS (30 s) → foto DESPUÉS
#   6:15  estado de los prestadores registrados; 45 s después, ningún PENDIENTE
#   ~8:00 fin de la carga                 → verificación de integridad
#
# Con retardo o 504 el Pod del aliado sigue vivo: su CPU y memoria casi no
# cambian (esperar no consume CPU). La fase «apagado» es la caída total: sus
# series de CPU y memoria se cortan y sus Pods disponibles caen a 0.
#
# Paneles (tablero «4 · Fallo 1»): circuito, llamadas al aliado, prestadores por
# estado de verificación, p95 del registro, CPU/memoria/Pods del aliado.
set -euo pipefail
source "$(dirname "$0")/comun.sh"

ALIADO="http://verificacion-externa.aws-local.svc.cluster.local:8010"
restaurar() {
  kubectl scale deploy/verificacion-externa -n "$NS" --replicas=1 >/dev/null 2>&1 || true
  kubectl rollout status deploy/verificacion-externa -n "$NS" --timeout=120s >/dev/null 2>&1 || true
  en_cluster DELETE "$ALIADO/_caos" >/dev/null 2>&1 || true
}
trap restaurar EXIT

iniciar_bitacora fallo1-dependencia-externa
restaurar
carga_de_fondo nominal DURACION=8m
espera 60 "línea base"
foto "ANTES (aliado sano)"

marca "INYECCIÓN: aliado con 8 000 ms de retardo"
en_cluster POST "$ALIADO/_caos" '{"latenciaMs": 8000}' | tee -a "$BITACORA"
espera 60 "tras 3 registros que esperan 2 s, el circuito se abre; los nuevos quedan PENDIENTE"
foto "DURANTE-LATENCIA (1 min)"
espera 60 "circuito abierto: los registros terminan al instante como PENDIENTE"
foto "DURANTE-LATENCIA (2 min)"

marca "INYECCIÓN: aliado respondiendo 504 Gateway Timeout"
en_cluster POST "$ALIADO/_caos" '{"latenciaMs": 0, "codigo": 504}' | tee -a "$BITACORA"
espera 90 "504: cada llamada de prueba falla al instante y el circuito vuelve a abrirse"
foto "DURANTE-504"

marca "INYECCIÓN: aliado APAGADO (kubectl scale deploy/verificacion-externa --replicas=0)"
kubectl scale deploy/verificacion-externa -n "$NS" --replicas=0 | tee -a "$BITACORA"
espera 60 "caída total: sin Pod, sus series de CPU y memoria se cortan"
foto "DURANTE-APAGADO"

marca "RECUPERACIÓN: aliado encendido y sin fallos"
restaurar
espera 45 "pasados 30 s, una llamada de prueba exitosa cierra el circuito"
foto "DESPUÉS (recuperado)"

marca "Prestadores registrados durante el experimento, por estado de verificación:"
psql_en confianza "SELECT estado, count(*) FROM sujetos_verificacion WHERE \"creadoEn\" >= '$INICIO_UTC' GROUP BY 1 ORDER BY 1" \
  | sed 's/^/   /' | tee -a "$BITACORA"
espera 45 "el reverificador (cada 15 s) resuelve a los PENDIENTE con el aliado sano"
marca "Tras la recuperación (no debería quedar ningún PENDIENTE):"
psql_en confianza "SELECT estado, count(*) FROM sujetos_verificacion WHERE \"creadoEn\" >= '$INICIO_UTC' GROUP BY 1 ORDER BY 1" \
  | sed 's/^/   /' | tee -a "$BITACORA"

esperar_carga
marca "Transiciones del circuito (logs de Confianza):"
kubectl logs -n "$NS" deploy/confianza --since=10m | grep circuit_breaker_cambio | tee -a "$BITACORA" || true
cerrar_con_integridad
