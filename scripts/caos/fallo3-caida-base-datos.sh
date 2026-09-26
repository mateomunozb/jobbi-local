#!/usr/bin/env bash
# FALLO 3 · Base de datos: PostgreSQL se cae durante unos minutos.
#
#   ./scripts/caos/fallo3-caida-base-datos.sh [segundos de caída, 180 por defecto]
#
# Hipótesis: durante la caída los endpoints responden 503 (reintentable) sin
# que ningún Pod se reinicie; el relay del outbox y los consumidores SQS dejan
# de tomar trabajo y esperan con backoff exponencial (1, 2, 4 … 30 s). Los
# eventos que llegan a SQS en ese lapso esperan en la cola: no gastan
# reintentos ni terminan en la DLQ. Al volver la base todo se drena solo y los
# saldos cuadran.
#
# 180 s no es casual: supera los ~150 s (5 recepciones × 30 s de visibilidad)
# que, antes del backoff, bastaban para mandar comisiones válidas a la DLQ.
#
# Para que haya eventos en la cola mientras la base está caída (el productor
# también depende de ella), el script publica N eventos CONTRATACION_COMPLETADA
# directamente en SNS durante la caída, para un prestador de prueba. Tras la
# recuperación deben aparecer los N cobrados, una sola vez cada uno.
#
# Paneles: errores 5xx (503), colas SQS, pool de BD y conexiones PostgreSQL,
# reinicios (deben quedar igual).
set -euo pipefail
source "$(dirname "$0")/comun.sh"

CAIDA="${1:-180}"
EVENTOS=20
PRESTADOR="caos-bd-$(date +%s)"
restaurar() {
  kubectl scale statefulset/postgres -n "$NS" --replicas=1 >/dev/null 2>&1 || true
}
trap restaurar EXIT

iniciar_bitacora fallo3-caida-base-datos
carga_de_fondo nominal DURACION=$((CAIDA / 60 + 5))m
espera 60 "línea base"
foto "ANTES"
REINICIOS_ANTES=$(prom 'sum(kube_pod_container_status_restarts_total{namespace="aws-local"})')

marca "INYECCIÓN: kubectl scale statefulset/postgres --replicas=0 (caída de ${CAIDA} s)"
kubectl scale statefulset/postgres -n "$NS" --replicas=0 | tee -a "$BITACORA"
kubectl wait --for=delete pod/postgres-0 -n "$NS" --timeout=120s >/dev/null 2>&1 || true
espera 15 "la base ya no responde"

marca "Publicando $EVENTOS eventos en SNS con la base caída (prestador $PRESTADOR)"
TEMA=$(kubectl exec -n "$NS" deploy/localstack -- awslocal sns list-topics --query \
       "Topics[?ends_with(TopicArn, ':contratacion-completada-topic')].TopicArn" --output text)
for i in $(seq 1 "$EVENTOS"); do
  kubectl exec -n "$NS" deploy/localstack -- awslocal sns publish --topic-arn "$TEMA" --message \
    "{\"eventoId\":\"caos-bd-$PRESTADOR-$i\",\"tipo\":\"CONTRATACION_COMPLETADA\",\"contratacionId\":\"$PRESTADOR-c$i\",\"prestadorId\":\"$PRESTADOR\",\"medioPago\":\"EFECTIVO\",\"valorAcordado\":10000,\"porcentajeComisionAplicado\":0.18,\"montoComision\":1800}" >/dev/null
done
espera $((CAIDA / 2 - 15)) "consumidores en backoff; los eventos esperan en SQS"
foto "DURANTE (mitad de la caída)"
marca "Logs de backoff (Monetización):"
kubectl logs -n "$NS" deploy/servicio-monetizacion --since=5m | grep -E "base_no_disponible|cobro_fallido" | tail -5 | tee -a "$BITACORA" || true
espera $((CAIDA - CAIDA / 2)) "resto de la caída"

marca "RECUPERACIÓN: kubectl scale statefulset/postgres --replicas=1"
kubectl scale statefulset/postgres -n "$NS" --replicas=1 | tee -a "$BITACORA"
INICIO=$SECONDS
kubectl wait --for=condition=Ready pod/postgres-0 -n "$NS" --timeout=300s >/dev/null
marca "PostgreSQL listo en $((SECONDS - INICIO)) s"
espera 60 "los hilos salen del backoff y drenan la cola"
foto "DESPUÉS"
marca "Reinicios de Pods: antes $REINICIOS_ANTES · después $(prom 'sum(kube_pod_container_status_restarts_total{namespace="aws-local"})')"

esperar_carga
COBRADOS=$(psql_en monetizacion "SELECT count(*) FROM movimientos WHERE tipo = 'COMISION' AND \"contratacionId\" LIKE '$PRESTADOR-c%'")
marca "Eventos publicados durante la caída: $EVENTOS · cobrados tras la recuperación: $COBRADOS (debe ser $EVENTOS)"
cerrar_con_integridad
[ "$COBRADOS" = "$EVENTOS" ]
