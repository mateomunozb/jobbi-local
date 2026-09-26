"""Genera los tableros de Grafana de JOBBI a partir de una sola biblioteca de paneles.

    python3 k8s/monitoring/grafana/generar_tableros.py && kubectl apply -k k8s/monitoring

Produce:
  - jobbi-caso-uso.json   el tablero general (3 filas: Negocio, RED, Infraestructura)
  - pruebas/NN-*.json     un tablero por prueba de docs/guia-de-pruebas.md, solo con
                          los paneles que esa prueba necesita y un recuadro que
                          explica qué mirar

Las series de infraestructura se agrupan por **servicio** (contenedor), no por
nombre de Pod: cada reinicio crea un Pod con otro nombre, y agrupar por Pod
llenaría las gráficas de curvas viejas. Así hay una curva por servicio aunque
el Pod se haya reemplazado muchas veces.
"""

from __future__ import annotations

import json
from pathlib import Path

AQUI = Path(__file__).parent
DS = {"type": "prometheus", "uid": "prometheus"}
H = "jobbi_http_request_duration_seconds"
NS = 'namespace="aws-local"'
USO = 'route=~".*check-out|.*check-in|.*acuerdos.*|.*billeteras.*|.*cobro"'
REGISTRO = "/api/auth/registro"
CHECKOUT = "/api/bff/contrataciones/{contratacion_id}/check-out"

# Ventana de las tasas y percentiles: 20 s (4 muestras con el scrape de 5 s). Con
# 1 min las gráficas tardaban ~1 min en reflejar un fallo; con 20 s se ve casi en
# vivo, a cambio de curvas algo más nerviosas. Las alertas siguen usando las
# reglas de 1 min de reglas.yml, para no dispararse por un pico de segundos.
ERRORES_PCT = (
    f'100 * (sum by (service) (rate({H}_count{{status=~"5.."}}[20s]))'
    f' or 0 * sum by (service) (rate({H}_count[20s])))'
    f' / (sum by (service) (rate({H}_count[20s])) > 0)'
)


# --- Piezas de un panel ---------------------------------------------------------------

def umbrales(*pasos):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in pasos]}


def linea_roja(valor):
    return {"thresholds": umbrales(("green", None), ("red", valor)),
            "custom": {"thresholdsStyle": {"mode": "line+area"}, "fillOpacity": 5}}


def panel(tipo, titulo, consultas, unidad="short", descripcion="", thr=None, extra=None):
    defaults = {"unit": unidad}
    if thr:
        defaults["thresholds"] = thr
    if extra:
        defaults.update(extra)
    p = {"type": tipo, "title": titulo, "description": descripcion, "datasource": DS,
         "targets": [{"datasource": DS, "expr": e, "legendFormat": l, "refId": chr(65 + i), "range": True}
                     for i, (e, l) in enumerate(consultas)],
         "fieldConfig": {"defaults": defaults, "overrides": []}, "options": {}}
    if tipo == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "area", "textMode": "value"}
        defaults.setdefault("color", {"mode": "thresholds"})
    elif tipo == "gauge":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "showThresholdMarkers": True}
    elif tipo == "timeseries":
        p["options"] = {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}
    return p


def escalonar(p, **series):
    """Dibuja como escalón punteado las series nombradas (capacidad asignada), con su color."""
    p["fieldConfig"]["overrides"] += [
        {"matcher": {"id": "byName", "options": nombre},
         "properties": [{"id": "custom.lineInterpolation", "value": "stepAfter"},
                        {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 6]}},
                        {"id": "custom.lineWidth", "value": 2},
                        {"id": "custom.fillOpacity", "value": 0},
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}
        for nombre, color in series.items()]
    return p


def texto(titulo, markdown):
    return {"type": "text", "title": titulo, "options": {"mode": "markdown", "content": markdown}}


def fila(titulo):
    return {"type": "row", "title": titulo, "collapsed": False, "panels": []}


# --- Consultas de infraestructura agrupadas por servicio ----------------------------------

def _filtro(contenedores: str | None) -> str:
    return f'{NS}, container!="", container!="POD"' + (f', container=~"{contenedores}"' if contenedores else "")


def _memoria_viva(filtro):
    """Working set solo del contenedor más reciente de cada Pod.

    Tras un reinicio en el mismo Pod (OOMKilled), cAdvisor sigue exportando ~5 min
    la serie del contenedor muerto congelada en su último valor; sumarla con la del
    nuevo daba memoria doble. Se queda la serie cuyo `id` tiene el inicio más reciente.
    """
    inicio = f"container_start_time_seconds{{{filtro}}}"
    return (f"(container_memory_working_set_bytes{{{filtro}}} and on (id) ({inicio}"
            f" == on (namespace, pod, container) group_left () max by (namespace, pod, container) ({inicio})))")


def mem(contenedores=None):
    return f'sum by (container) ({_memoria_viva(_filtro(contenedores))})'


def cpu(contenedores=None):
    f = f'{NS}' + (f', container=~"{contenedores}"' if contenedores else "")
    return f'sum by (container) (node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate{{{f}}})'


def pct_limite(recurso, contenedores=None):
    if recurso == "memory":
        uso = _memoria_viva(_filtro(contenedores))
    else:
        f = f'{NS}' + (f', container=~"{contenedores}"' if contenedores else "")
        uso = f"node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate{{{f}}}"
    lim = f'{NS}, resource="{recurso}"' + (f', container=~"{contenedores}"' if contenedores else "")
    return (f'max by (container) (100 * sum by (namespace, pod, container) ({uso})'
            f' / on (namespace, pod, container) sum by (namespace, pod, container)'
            f' (kube_pod_container_resource_limits{{{lim}}}))')


BD = f'{NS}, container="postgres"'


def bd_asignada(recurso, tipo="limits"):
    """Lo que Kubernetes le asignó a PostgreSQL (límite o reserva), según kube-state-metrics."""
    return f'max(kube_pod_container_resource_{tipo}{{{BD}, resource="{recurso}"}})'


BD_CPU = f"sum(rate(container_cpu_usage_seconds_total{{{BD}}}[20s]))"
# max: tras un reinicio, cAdvisor mantiene un rato la serie del contenedor anterior.
BD_MEM = f"max(container_memory_working_set_bytes{{{BD}}})"


def red(sentido, pods=None):
    # Las métricas de red son por Pod: se renombran al Deployment quitando el sufijo.
    f = f'{NS}' + (f', pod=~"{pods}"' if pods else "")
    base = (f'sum by (servicio) (label_replace(rate(container_network_{sentido}_bytes_total{{{f}}}[20s]),'
            f' "servicio", "$1", "pod", "(.+?)(-[a-z0-9]{{8,10}}-[a-z0-9]{{5}}|-[0-9]+)$"))')
    return base if sentido == "receive" else f"-{base}"


# --- Biblioteca de paneles ---------------------------------------------------------------------
# Los totales de negocio que salen de la base (saldo, bloqueadas, comisiones, outbox, conexiones)
# los publica cada réplica del servicio: se agregan con max, no con sum, para no contarlos una vez
# por Pod cuando Monetización escala a varias réplicas.

P = {
    # Negocio
    "recaudo": lambda: panel("stat", "Tasa de recaudo de comisión en efectivo",
        [("jobbi:recaudo_comision_efectivo:porcentaje", "recaudo")], "percent",
        "Comisiones cargadas en billetera ÷ comisiones facturadas en efectivo. Meta ≥ 95 %.",
        umbrales(("red", None), ("yellow", 90), ("green", 95)), {"decimals": 1, "max": 100}),
    "dlq": lambda: panel("stat", "Eventos en Dead Letter Queue", [("jobbi:eventos_en_dlq", "DLQ")], "short",
        "Mensajes apartados tras 5 intentos. Meta = 0; > 0 es alerta P2.", umbrales(("green", None), ("red", 1))),
    "match": lambda: panel("stat", "Tasa de contacto tras búsqueda (match rate)",
        [("jobbi:match_rate:porcentaje", "match")], "percent", "Contactos ÷ búsquedas. Meta ≥ 35 %.",
        umbrales(("red", None), ("yellow", 25), ("green", 35)), {"decimals": 1}),
    "match_serie": lambda: panel("timeseries", "Búsquedas y contactos tras búsqueda (por minuto) · match rate",
        [("sum(delta(jobbi_busquedas_registradas[1m]))", "búsquedas/min"),
         ("sum(delta(jobbi_contactos_iniciados[1m]))", "contactos tras búsqueda/min"),
         ("jobbi:match_rate:porcentaje", "match rate acumulado (%)")], "short",
        "Cada búsqueda del demandante suma al denominador; cada contacto nuevo que la cita, al numerador. "
        "Meta del match rate ≥ 35 %."),
    "bloqueadas": lambda: panel("stat", "Billeteras bloqueadas (RN-04)", [("max(jobbi_billeteras_bloqueadas)", "bloqueadas")],
        "short", "Prestadores cuyo saldo pendiente alcanzó el umbral.", umbrales(("green", None), ("orange", 1))),
    "saldo": lambda: panel("stat", "Saldo pendiente total", [("max(jobbi_saldo_pendiente_pesos)", "saldo")],
        "currencyUSD", "Comisiones en efectivo que los prestadores deben (COP).", umbrales(("blue", None)), {"decimals": 0}),
    "comisiones": lambda: panel("timeseries", "Comisiones facturadas vs. cargadas en billetera (COP)",
        [("max(jobbi_comisiones_facturadas_efectivo_pesos)", "facturadas (Contrataciones)"),
         ("max(jobbi_comisiones_cobradas_pesos)", "cargadas en billetera (Monetización)")], "currencyUSD",
        "La distancia entre las dos curvas es lo que aún viaja por outbox → SNS → SQS."),
    "consumidos": lambda: panel("timeseries", "Eventos consumidos por resultado (por minuto)",
        [("sum by (consumidor, resultado) (rate(jobbi_eventos_consumidos_total[20s])) * 60", "{{consumidor}} · {{resultado}}")],
        "short", "COMISION_COBRADA, DUPLICADO (Idempotent Receiver), NOTIFICADO, ERROR."),
    # OKR del negocio que el sistema puede medir hoy (A1, B3, C2, C3)
    "okr_a1": lambda: panel("stat", "A1 · Prestadores registrados con verificación aprobada",
        [('100 * sum(jobbi_prestadores_verificacion{estado="APROBADA"}) / (sum(jobbi_prestadores_verificacion) > 0)',
          "verificados")], "percent",
        "OKR Confianza · KR1: 100 % de los prestadores activos verificados (identidad + antecedentes). Por RN-01 un "
        "prestador sin verificación aprobada no aparece ni contrata, así que todo prestador activo está verificado. "
        "Este % muestra cuántos de los registrados superaron la verificación (el resto está pendiente o fue rechazado).",
        umbrales(("blue", None)), {"decimals": 1, "max": 100}),
    "okr_b3": lambda: panel("stat", "B3 · Match rate (contacto tras búsqueda)",
        [("jobbi:match_rate:porcentaje", "match")], "percent",
        "OKR Liquidez · KR3: tasa de contacto tras búsqueda ≥ 35 % mensual. Contactos que citan una búsqueda ÷ búsquedas.",
        umbrales(("red", None), ("yellow", 25), ("green", 35)), {"decimals": 1}),
    "okr_c2": lambda: panel("stat", "C2 · Ingresos por comisión cobrados (COP)",
        [("max(jobbi_comisiones_cobradas_pesos)", "comisiones")], "currencyUSD",
        "OKR Viabilidad · KR2: $20.000.000 COP mensuales por comisión + suscripción. Aquí, el acumulado de comisiones "
        "ya cargadas en billeteras (aún no separa por mes ni suma suscripciones).",
        umbrales(("blue", None)), {"decimals": 0}),
    "okr_c3": lambda: panel("stat", "C3 · Tasa de recaudo de comisión en efectivo",
        [("jobbi:recaudo_comision_efectivo:porcentaje", "recaudo")], "percent",
        "OKR Viabilidad · KR3: recaudo ≥ 95 % mensual. Comisiones cargadas en billetera ÷ comisiones facturadas en "
        "efectivo. Si baja de 95 % se dispara la alerta RecaudoBajo.",
        umbrales(("red", None), ("yellow", 90), ("green", 95)), {"decimals": 1, "max": 100}),
    "okr_c3_serie": lambda: panel("timeseries", "C3 · Recaudo en el tiempo (meta ≥ 95 %)",
        [("jobbi:recaudo_comision_efectivo:porcentaje", "recaudo")], "percent",
        "Baja un instante mientras un cobro viaja por outbox → SNS → SQS y vuelve a ~100 % cuando llega. Debajo de la "
        "franja verde (95 %) el KR está en riesgo.",
        extra={"thresholds": umbrales(("red", None), ("green", 95)),
               "custom": {"thresholdsStyle": {"mode": "line+area"}, "fillOpacity": 5}, "min": 80, "max": 101}),
    # RED
    "rate": lambda: panel("timeseries", "Rate · solicitudes por segundo (gateway)",
        [(f'sum by (route) (rate({H}_count{{service="gateway", {USO}}}[20s]))', "{{route}}"),
         (f'sum(rate({H}_count{{service="gateway"}}[20s]))', "TOTAL gateway")], "reqps",
        "Throughput por endpoint del caso de uso. Nominal ≈ 12 TPS."),
    "errores": lambda: panel("timeseries", "Errors · % de respuestas 5xx por servicio",
        [(ERRORES_PCT, "{{service}}")], "percent", "SLO < 1 % (línea roja).", extra=linea_roja(1)),
    "latencia": lambda: panel("timeseries", "Duration · latencia del gateway (p50 / p95 / p99)",
        [(f'histogram_quantile({q}, sum by (le) (rate({H}_bucket{{service="gateway"}}[20s]))) * 1000', f"p{int(q * 100)}")
         for q in (0.5, 0.95, 0.99)], "ms", "SLO p95 < 400 ms (línea roja). Mira el p95.", extra=linea_roja(400)),
    "p95_endpoint": lambda: panel("timeseries", "Duration · p95 por endpoint del caso de uso (gateway)",
        [(f'histogram_quantile(0.95, sum by (le, route) (rate({H}_bucket{{service="gateway", {USO}}}[20s]))) * 1000', "{{route}}")],
        "ms", "Cada curva es un paso del caso de uso visto desde el gateway.", extra=linea_roja(400)),
    "p95_checkout": lambda: panel("timeseries", "p95 · check-out (no pasa por el aliado)",
        [(f'histogram_quantile(0.95, sum by (le) (rate({H}_bucket{{service="gateway", route="{CHECKOUT}"}}[20s]))) * 1000', "p95 check-out")],
        "ms", "Debe mantenerse igual durante el fallo del aliado.", extra=linea_roja(400)),
    "cobro": lambda: panel("timeseries", "Latencia del cobro asíncrono (check-out → billetera)",
        [(f'histogram_quantile({q}, sum by (le) (rate(jobbi_cobro_latencia_seconds_bucket[20s])))', f"p{int(q * 100)}")
         for q in (0.5, 0.95)], "s", "Ventana de consistencia eventual: outbox + relay + SQS + worker."),
    "outbox": lambda: panel("stat", "Saturación · backlog del outbox", [("sum(max by (productor) (jobbi_outbox_pendientes)) or vector(0)", "pendientes")],
        "short", "Eventos guardados sin publicar. Meta < 10; alerta > 30.", umbrales(("green", None), ("yellow", 10), ("red", 30))),
    # Las colas las exportan su consumidor y el gateway: max, no sum, para no contarlas dos veces.
    "colas": lambda: panel("timeseries", "Saturación · mensajes en colas SQS",
        [('max by (cola) (jobbi_sqs_mensajes{estado="visibles"})', "{{cola}}")], "short",
        "Mensajes esperando en cada cola. Las *-dlq deben quedarse en 0."),
    "cola_monetizacion": lambda: panel("timeseries", "Cola de Monetización · visibles vs. en vuelo",
        [('max by (estado) (jobbi_sqs_mensajes{cola="monetizacion-events-queue"})', "{{estado}}"),
         ('max(jobbi_sqs_mensajes{cola="monetizacion-events-dlq", estado="visibles"})', "DLQ")], "short",
        "visibles: esperan a un consumidor (suben mientras no hay Pod). en_vuelo: entregados a un worker y aún sin "
        "confirmar; si ese worker murió, reaparecen como visibles al vencer el VisibilityTimeout (30 s). "
        "La mide el gateway, así que no se corta cuando muere el Pod de Monetización.",
        extra={"custom": {"drawStyle": "bars", "fillOpacity": 60}}),
    "flujo_cobro": lambda: panel("timeseries", "Flujo del cobro · publicados en SNS vs. consumidos (por minuto)",
        [('sum(rate(jobbi_eventos_publicados_total{tema="contratacion-completada-topic"}[20s])) * 60',
          "publicados por el outbox de Contrataciones"),
         ('sum(rate(jobbi_eventos_consumidos_total{consumidor="monetizacion"}[20s])) * 60',
          "consumidos por Monetización")], "short",
        "Si los publicados siguen y los consumidos caen a 0, la diferencia es lo que se acumula en la cola; "
        "al volver el consumidor aparece un pico que la drena."),
    "outbox_serie": lambda: panel("timeseries", "Outbox · eventos pendientes de publicar por productor",
        [("max by (productor) (jobbi_outbox_pendientes)", "{{productor}}"),
         ('max(jobbi_outbox_pendiente_mas_antiguo_segundos{productor="contrataciones"})', "edad del más antiguo (s)")],
        "short", "Sube solo si SNS o la base del productor fallan. Que el consumidor muera no lo afecta: el relay "
        "publica en SNS sin saber quién escucha."),
    "cb_estado": lambda: panel("stat", "Circuit Breaker · aliado de verificación",
        [('max(jobbi_circuit_breaker_estado{dependencia="verificacion-externa"}) or vector(0)', "estado")], "short",
        "0 CERRADO · 1 SEMIABIERTO · 2 ABIERTO. Se abre tras 3 fallos (timeout 2 s); prueba de nuevo a los 30 s.",
        umbrales(("green", None), ("yellow", 1), ("red", 2)),
        {"mappings": [{"type": "value", "options": {"0": {"text": "CERRADO", "color": "green"},
                                                     "1": {"text": "SEMIABIERTO", "color": "yellow"},
                                                     "2": {"text": "ABIERTO", "color": "red"}}}]}),
    "cb_llamadas": lambda: panel("timeseries", "Llamadas al aliado de verificación por resultado (/s)",
        [("sum by (resultado) (rate(jobbi_dependencia_externa_llamadas_total[20s]))", "{{resultado}}")], "reqps",
        "exito / fallo (timeout o 5xx) / rechazada_por_circuito (respuesta inmediata con el fallback)."),
    "p95_registro": lambda: panel("timeseries", "p95 · registro de usuarios (incluye la verificación RN-01)",
        [(f'histogram_quantile(0.95, sum by (le) (rate({H}_bucket{{service="gateway", route="{REGISTRO}"}}[20s]))) * 1000', "p95 registro"),
         (f'histogram_quantile(0.95, sum by (le) (rate({H}_bucket{{service="confianza", route="/prestadores/{{prestador_id}}/verificacion"}}[20s]))) * 1000',
          "p95 verificación en Confianza")], "ms",
        "Al registrarse, el prestador se verifica con el aliado. Sin Circuit Breaker, cada registro esperaría los 8 s del aliado.",
        extra=linea_roja(400)),
    "verificacion": lambda: panel("timeseries", "Verificación de identidad por estado (RN-01)",
        [("sum by (estado) (jobbi_prestadores_verificacion)", "prestadores · {{estado}}"),
         ("sum by (estado) (jobbi_demandantes_verificacion)", "demandantes · {{estado}}")], "short",
        "APROBADA: trabaja y contrata. RECHAZADA: no. PENDIENTE: el aliado no respondió al registrarse; el "
        "reverificador lo resuelve cuando el aliado vuelve. Aplica a prestadores y demandantes."),
    # Infraestructura (por servicio)
    "mem": lambda c=None: panel("timeseries", "Uso de memoria por servicio (working set)", [(mem(c), "{{container}}")],
        "bytes", "Una curva por servicio, aunque su Pod se haya reemplazado."),
    "mem_pct": lambda c=None: panel("gauge", "Memoria · % del límite por servicio", [(pct_limite("memory", c), "{{container}}")],
        "percent", "Meta: < 80 % sostenido.", umbrales(("green", None), ("yellow", 80), ("red", 90)), {"min": 0, "max": 100}),
    "mem_pct_serie": lambda c=None: panel("timeseries", "Memoria · % del límite en el tiempo", [(pct_limite("memory", c), "{{container}}")],
        "percent", "La rampa hacia el 100 % y la caída al morir el contenedor.", extra=linea_roja(100)),
    "cpu": lambda c=None: panel("timeseries", "Uso de CPU por servicio (núcleos)", [(cpu(c), "{{container}}")], "short",
        "Regla de grabación node_namespace_pod_container:…:sum_rate, sumada por servicio."),
    "cpu_pct": lambda c=None: panel("timeseries", "CPU · % del límite por servicio", [(pct_limite("cpu", c), "{{container}}")],
        "percent", "Al llegar al 100 %, Kubernetes frena el servicio (throttling).", extra=linea_roja(80)),
    "red": lambda p=None: panel("timeseries", "Network I/O por servicio (recibido + / enviado −)",
        [(red("receive", p), "rx {{servicio}}"), (red("transmit", p), "tx {{servicio}}")], "Bps",
        "Tráfico de red de cada servicio."),
    "reinicios": lambda c=None: panel("timeseries", "Reinicios de contenedores por servicio",
        [(f'sum by (container) (kube_pod_container_status_restarts_total{{{NS}' + (f', container=~"{c}"' if c else "") + "})",
          "{{container}}")], "short", "Sube cuando un contenedor muere y Kubernetes lo reinicia en el mismo Pod."),
    "reinicios_total": lambda: panel("stat", "Reinicios de contenedores (todo el sistema)",
        [(f"sum(kube_pod_container_status_restarts_total{{{NS}}}) or vector(0)", "reinicios")], "short",
        "Total acumulado. Debe quedarse igual durante el fallo: los servicios esperan con backoff, no se reinician.",
        umbrales(("blue", None))),
    "reinicios_monetizacion": lambda: panel("stat", "Reinicios de Monetización",
        [(f'sum(kube_pod_container_status_restarts_total{{{NS}, container="monetizacion"}}) or vector(0)', "reinicios")],
        "short", "Sube cada vez que el contenedor muere y Kubernetes lo reinicia en el mismo Pod.",
        umbrales(("green", None), ("orange", 1))),
    "oom": lambda: panel("stat", "Contenedores terminados por OOMKilled",
        [(f'sum(kube_pod_container_status_last_terminated_reason{{{NS}, reason="OOMKilled"}}) or vector(0)', "OOMKilled")],
        "short", "Motivo de la última terminación según kube-state-metrics.", umbrales(("green", None), ("red", 1))),
    "disponibles": lambda: panel("timeseries", "Pods disponibles de Monetización",
        [('sum(kube_deployment_status_replicas_available{namespace="aws-local", deployment="servicio-monetizacion"})', "disponibles")],
        "short", "Cae a 0 al eliminar el Pod y vuelve a 1 cuando el reemplazo está listo."),
    "disponibles_aliado": lambda: panel("timeseries", "Pods disponibles · aliado, Confianza y gateway",
        [('sum by (deployment) (kube_deployment_status_replicas_available{namespace="aws-local", '
          'deployment=~"verificacion-externa|confianza|api-gateway"})', "{{deployment}}")],
        "short", "El aliado cae a 0 en la fase «apagado» del Fallo 1; Confianza y el gateway siguen en 1."),
    "pool": lambda: panel("timeseries", "Pool de BD · conexiones en uso por servicio",
        [('sum by (app) (jobbi_db_pool_conexiones{estado="en_uso"})', "{{app}}"),
         ("max(jobbi_db_pool_capacidad)", "capacidad por servicio (5 + 10)")], "short",
        "Si llega a la capacidad, las peticiones esperan turno y a los 30 s responden 503."),
    # Auto-escalado de la base (simulación de Aurora Serverless v2, Fallo 3)
    "bd_acu": lambda: panel("stat", "Capacidad de la base (ACU)", [("max(jobbi_bd_capacidad_acu)", "ACU")], "short",
        "Nivel que asignó el escalador: 0,5 (normal) → 1 → 2 → 4. 1 ACU local ≈ 1 núcleo y ~1 GiB.",
        umbrales(("green", None), ("yellow", 1), ("orange", 2), ("red", 4)), {"decimals": 1}),
    "bd_cpu_asignada": lambda: panel("stat", "CPU asignada a PostgreSQL (núcleos)", [(bd_asignada("cpu"), "núcleos")],
        "none", "Límite de CPU que Kubernetes aplicó al contenedor (kube-state-metrics). Cambia sin reiniciar el Pod.",
        umbrales(("blue", None)), {"decimals": 2}),
    "bd_mem_asignada": lambda: panel("stat", "Memoria asignada a PostgreSQL", [(bd_asignada("memory"), "límite")], "bytes",
        "Límite de memoria que Kubernetes aplicó al contenedor.", umbrales(("purple", None))),
    "bd_work_mem": lambda: panel("stat", "work_mem por operación", [("max(jobbi_bd_work_mem_bytes)", "work_mem")], "bytes",
        "Memoria de ordenamiento por operación que el escalador fija con ALTER SYSTEM: la base aprovecha la memoria nueva.",
        umbrales(("text", None))),
    "bd_reinicios": lambda: panel("stat", "Reinicios de PostgreSQL",
        [(f"sum(kube_pod_container_status_restarts_total{{{BD}}}) or vector(0)", "reinicios")], "short",
        "Debe quedarse igual: el escalado es en caliente.", umbrales(("green", None), ("red", 1))),
    "bd_cpu": lambda: escalonar(panel("timeseries", "PostgreSQL · CPU usada vs. asignada (núcleos)",
        [(BD_CPU, "CPU usada"), (bd_asignada("cpu"), "CPU asignada (límite)"),
         (bd_asignada("cpu", "requests"), "CPU reservada (request)")], "short",
        "La línea punteada es la capacidad: sube en escalones cuando la CPU usada se pega al límite y baja cuando "
        "pasa la saturación. Mientras la usada toca el límite, la base está frenada (ver throttling)."),
        **{"CPU asignada (límite)": "red", "CPU reservada (request)": "orange"}),
    "bd_mem": lambda: escalonar(panel("timeseries", "PostgreSQL · memoria usada vs. asignada",
        [(BD_MEM, "memoria usada"), (bd_asignada("memory"), "memoria asignada (límite)"),
         (bd_asignada("memory", "requests"), "memoria reservada (request)")], "bytes",
        "La memoria asignada sube y baja con la capacidad; la usada crece con la carga porque cada nivel permite "
        "más work_mem por ordenamiento, y se libera al terminar la saturación."),
        **{"memoria asignada (límite)": "purple", "memoria reservada (request)": "orange"}),
    "bd_utilizacion": lambda: panel("timeseries", "PostgreSQL · % de la CPU asignada en uso",
        [(f"100 * {BD_CPU} / {bd_asignada('cpu')}", "% de la CPU asignada")], "percent",
        "Por encima de la línea roja (75 %) durante 10 s, la base sube un nivel. Baja cuando lo que usa cabría "
        "holgado (≤ 50 %) en el nivel inferior durante 30 s.", extra={**linea_roja(75), "max": 110, "min": 0}),
    "bd_throttling": lambda: panel("timeseries", "PostgreSQL · CPU frenada por su límite (throttling)",
        [(f"100 * sum(rate(container_cpu_cfs_throttled_periods_total{{{BD}}}[20s])) / "
          f"sum(rate(container_cpu_cfs_periods_total{{{BD}}}[20s]))", "% de periodos frenados")], "percent",
        "Qué tanto el kernel frenó a PostgreSQL por pedir más CPU de la asignada: es la saturación. Cae al subir "
        "la capacidad.", extra={"max": 100, "min": 0}),
    "p95_checkout_bd": lambda: panel("timeseries", "Impacto en el usuario · p95 del check-out (lee y escribe en la base)",
        [(f'histogram_quantile(0.95, sum by (le) (rate({H}_bucket{{service="gateway", route="{CHECKOUT}"}}[20s]))) * 1000',
          "p95 check-out"),
         (f'histogram_quantile(0.95, sum by (le) (rate({H}_bucket{{service="gateway"}}[20s]))) * 1000', "p95 gateway")],
        "ms", "Puede subir mientras la base está frenada y vuelve a bajar cuando el escalador le da capacidad.",
        extra=linea_roja(400)),
    "postgres": lambda: panel("timeseries", "PostgreSQL · conexiones de clientes vs. max_connections",
        [("max by (estado) (jobbi_postgres_conexiones)", "{{estado}}"), ("max(jobbi_postgres_conexiones_max)", "max_connections")],
        "short", "Hueco = la base no respondió (Fallo 3)."),
    # Auto-escalado horizontal de Monetización por la cola (KEDA + HPA)
    "as_replicas": lambda: escalonar(panel("timeseries", "Réplicas de Monetización · pedidas por el HPA vs. listas",
        [(HPA_DESEADAS, "deseadas por el HPA"), (MON_LISTAS, "listas (consumiendo)"), (HPA_MAX, "máximo (3)")], "short",
        "El HPA (que alimenta KEDA con la cola) decide cuántas réplicas hacen falta; «listas» sube unos segundos "
        "después, cuando el Pod nuevo arranca y pasa su readiness. Sube de a 1 cada 30 s como máximo y baja de a 1 "
        "por minuto tras 3 min de calma.", extra={"min": 0, "max": 3.5, "decimals": 0}),
        **{"deseadas por el HPA": "orange", "máximo (3)": "red"}),
    "as_replicas_stat": lambda: panel("stat", "Réplicas listas ahora", [(MON_LISTAS, "réplicas")], "short",
        "Pods de Monetización consumiendo la cola.", umbrales(("green", None), ("yellow", 2), ("orange", 3)), {"decimals": 0}),
    "as_cola": lambda: escalonar(panel("timeseries", "Cola de Monetización · mensajes esperando vs. umbral para pedir otro Pod",
        [('max(jobbi_sqs_mensajes{cola="monetizacion-events-queue", estado="visibles"})', "mensajes esperando"),
         (f"{HPA_OBJETIVO} * scalar({MON_LISTAS})", "umbral: réplicas × 100")], "short",
        "Regla: réplicas = ⌈esperando ÷ 100⌉. Cuando la curva azul pasa la punteada (con ~10 % de tolerancia), el "
        "HPA pide un Pod más; la punteada sube con él y la cola empieza a bajar."),
        **{"umbral: réplicas × 100": "red"}),
    "as_consumo_pod": lambda: panel("timeseries", "Consumo por Pod (mensajes/s) · llegan vs. se procesan",
        [('sum by (pod) (rate(jobbi_eventos_consumidos_total{consumidor="monetizacion"}[20s]))', "{{pod}}"),
         ('sum(rate(jobbi_eventos_consumidos_total{consumidor="monetizacion"}[20s]))', "TOTAL procesados"),
         ('sum(rate(jobbi_eventos_consumidos_total{consumidor="monetizacion"}[20s])) + '
          'deriv(max(jobbi_sqs_mensajes{cola="monetizacion-events-queue", estado="visibles"})[30s:5s])',
          "llegan (estimado)")], "short",
        "Cada Pod procesa ~7 msg/s durante la prueba (1 hilo, 125 ms simulados por mensaje). «llegan» = procesados "
        "+ lo que crece la cola: mientras supere al TOTAL, la cola sube; al llegar un Pod nuevo, el TOTAL lo alcanza."),
    "as_cpu_pod": lambda: panel("timeseries", "CPU por Pod de Monetización (núcleos)",
        [(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{NS}, container="monetizacion"}}[20s]))', "{{pod}}")],
        "short", "Casi plana aunque la cola crezca: el worker espera más de lo que calcula. Por eso se escala por la "
        "cola y no por CPU."),
}

HPA_SEL = 'namespace="aws-local", horizontalpodautoscaler="monetizacion-cola"'
HPA_DESEADAS = f"max(kube_horizontalpodautoscaler_status_desired_replicas{{{HPA_SEL}}})"
HPA_MAX = f"max(kube_horizontalpodautoscaler_spec_max_replicas{{{HPA_SEL}}})"
HPA_OBJETIVO = f"max(kube_horizontalpodautoscaler_spec_target_metric{{{HPA_SEL}}})"
MON_LISTAS = 'sum(kube_deployment_status_replicas_ready{namespace="aws-local", deployment="servicio-monetizacion"})'


# Marcas verticales en todas las gráficas del tablero, en el instante de cada escalado.
ANOTACION_ESCALADO = {
    "datasource": DS, "enable": True, "iconColor": "orange", "name": "Escalado de la base",
    "expr": "sum by (direccion) (increase(jobbi_bd_escalados_total[10s])) > 0", "step": "5s",
    "titleFormat": "Base: {{direccion}} de capacidad", "tagKeys": "direccion",
}


def armar(uid, titulo, descripcion, bloques, desde="now-15m", etiquetas=("jobbi",), anotaciones=()):
    """bloques: lista de filas; cada fila es [(panel, ancho, alto), …] o un panel de fila (row)."""
    panels, y, pid = [], 0, 0
    for bloque in bloques:
        if isinstance(bloque, dict):  # fila
            pid += 1
            panels.append({**bloque, "id": pid, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}})
            y += 1
            continue
        x, alto = 0, max(h for _, _, h in bloque)
        for p, w, h in bloque:
            pid += 1
            panels.append({**p, "id": pid, "gridPos": {"h": h, "w": w, "x": x, "y": y}})
            x += w
        y += alto
    return {"uid": uid, "title": titulo, "description": descripcion, "tags": list(etiquetas),
            "timezone": "browser", "schemaVersion": 39, "version": 1, "refresh": "5s",
            "time": {"from": desde, "to": "now"}, "editable": True, "panels": panels,
            "templating": {"list": []}, "annotations": {"list": list(anotaciones)}}


def guia(comando, caso, mirar, exito, extra=""):
    md = (f"**Comando:** `{comando}`  \n**Caso del documento:** {caso}\n\n"
          f"**Qué deberías ver:** {mirar}\n\n**Criterio de éxito:** {exito}")
    return texto("Qué es esta prueba", md + (f"\n\n{extra}" if extra else ""))


OKR_EXPLICACION = """\
Cuatro de los KR del caso de negocio se miden con datos reales del sistema. Los demás (A2, A3, B1, B2, C1, D1, D2) aún
no tienen métrica; C4 (CAC) y D3 (NPS) dependen de datos externos.

| KR | Meta | Qué muestra aquí | Cómo se mueve en la demo |
|---|---|---|---|
| **A1 · Confianza** | 100 % de prestadores activos verificados | % de registrados con identidad y antecedentes aprobados; abajo, prestadores y demandantes por estado | Con el aliado caído (Fallo 1) crecen los PENDIENTE: no aparecen ni contratan hasta verificarse. Ningún activo queda sin verificar (RN-01) |
| **B3 · Liquidez** | Match rate ≥ 35 % mensual | Contactos que salen de una búsqueda ÷ búsquedas | Sube y baja en vivo con la carga de usuarios (nominal o pico50) |
| **C2 · Viabilidad** | $20 M COP/mes (comisión + suscripción) | Comisiones ya cargadas en billeteras (acumulado) y su comparación con lo facturado | Crece con cada servicio completado |
| **C3 · Viabilidad** | Recaudo ≥ 95 % mensual | Comisiones cobradas ÷ facturadas en efectivo | Se mantiene ~100 % aunque el sistema se estrese o falle un servicio: lo facturado termina igual a lo cobrado |
"""


# --- Tablero general -------------------------------------------------------------------------------

def tablero_general():
    return armar("jobbi-caso-uso", "JOBBI · Cobro de comisión en efectivo",
        "Registrar el pago en efectivo de una contratación completada y aplicar la comisión pendiente.", [
            fila("Negocio · Cobro de comisión en efectivo (tabla 8.1.3)"),
            [(P["recaudo"](), 5, 5), (P["dlq"](), 5, 5), (P["match"](), 5, 5), (P["bloqueadas"](), 4, 5), (P["saldo"](), 5, 5)],
            [(P["comisiones"](), 12, 8), (P["consumidos"](), 12, 8)],
            fila("OKR del negocio"),
            [(texto("Qué muestra cada OKR", OKR_EXPLICACION), 24, 8)],
            [(P["okr_a1"](), 6, 6), (P["okr_b3"](), 6, 6), (P["okr_c2"](), 6, 6), (P["okr_c3"](), 6, 6)],
            [(P["verificacion"](), 12, 8), (P["match_serie"](), 12, 8)],
            [(P["comisiones"](), 12, 8), (P["okr_c3_serie"](), 12, 8)],
            fila("Técnicas · RED + saturación (tabla 8.1.2)"),
            [(P["rate"](), 8, 8), (P["errores"](), 8, 8), (P["latencia"](), 8, 8)],
            [(P["p95_endpoint"](), 8, 8), (P["cobro"](), 8, 8), (P["outbox"](), 4, 8), (P["colas"](), 4, 8)],
            [(P["cb_estado"](), 6, 7), (P["cb_llamadas"](), 10, 7), (P["p95_registro"](), 8, 7)],
            [(P["verificacion"](), 24, 7)],
            fila("Infraestructura · servicios en Minikube, namespace aws-local (tabla 8.1.4)"),
            [(P["mem"](), 12, 8), (P["mem_pct"](), 12, 8)],
            [(P["cpu"](), 12, 8), (P["cpu_pct"](), 12, 8)],
            [(P["red"](), 12, 8), (P["reinicios"](), 8, 8), (P["oom"](), 4, 8)],
            [(P["pool"](), 12, 8), (P["postgres"](), 12, 8)],
        ])


# --- Un tablero por prueba (docs/guia-de-pruebas.md) ---------------------------------------------------

# Cada tablero muestra solo los indicadores que cuentan la historia de esa prueba:
# arriba los números que dan el veredicto (stat) y debajo las gráficas del fallo.
# Menos paneles = se entiende de un vistazo en una demostración en vivo.
PRUEBAS = [
    ("01-humo", "1 · Humo (preparación)", "./scripts/k6-en-cluster.sh humo", "Ninguno: comprobación previa.",
     "un pulso pequeño de tráfico en **Rate**, **Errores** en 0 % y la serie COMISION_COBRADA en **Eventos consumidos**.",
     "✓ en todos los checks de k6 y consistencia 100 %.",
     [[(P["rate"](), 12, 9), (P["errores"](), 12, 9)],
      [(P["consumidos"](), 24, 8)]]),
    ("02-nominal", "2 · Nominal · 12 TPS", "./scripts/k6-en-cluster.sh nominal", "Punto 10.4 · Escenario 1 Nominal.",
     "**Recaudo** ~100 % y **DLQ** en 0; ~12 req/s estables en **Rate**; el **p95** muy por debajo de la línea roja "
     "(400 ms) y **Errores** en 0 %.",
     "k6 termina con «umbrales cumplidos» (p95 check-out < 300 ms, errores < 1 %, consistencia 100 %).",
     [[(P["recaudo"](), 12, 5), (P["dlq"](), 12, 5)],
      [(P["rate"](), 12, 9), (P["latencia"](), 12, 9)],
      [(P["errores"](), 24, 7)]]),
    ("03-pico", "3 · Pico · ráfaga de 50 TPS", "./scripts/k6-en-cluster.sh pico50", "Punto 10.4 · Escenario 2 Pico.",
     "el escalón 12 → 50 → 12 en **Rate**; el **p95** cruza la línea roja durante la ráfaga y vuelve a bajar; en "
     "**Comisiones** las dos curvas se separan un momento y terminan juntas; **DLQ** en 0.",
     "sin degradación financiera: consistencia 100 % y DLQ = 0, aunque la latencia no cumpla.",
     [[(P["recaudo"](), 12, 5), (P["dlq"](), 12, 5)],
      [(P["rate"](), 12, 9), (P["latencia"](), 12, 9)],
      [(P["comisiones"](), 24, 8)]]),
    ("04-fallo1", "4 · Fallo 1 · Red: el aliado de verificación se cae", "./scripts/caos/fallo1-dependencia-externa.sh",
     "Punto 11 · Fallo 1 (Red / dependencia externa).",
     "el **Circuit Breaker** pasa de CERRADO a ABIERTO (rojo) al caer el aliado y vuelve a CERRADO al restaurarlo; los "
     "**Pods del aliado** caen a 0 y vuelven a 1; en **Verificación** crece PENDIENTE durante la caída y baja a 0 "
     "cuando el reverificador aprueba a los pendientes; el **p95 del registro** no se dispara: nadie se queda colgado.",
     "el circuito se abre y se cierra solo, los PENDIENTE se resuelven solos e integridad OK.",
     [[(P["cb_estado"](), 8, 7), (P["disponibles_aliado"](), 16, 7)],
      [(P["verificacion"](), 12, 9), (P["p95_registro"](), 12, 9)]]),
    ("05-fallo2", "5 · Fallo 2 · Servicios: muere el Pod de Monetización",
     "./scripts/caos/fallo2-eliminar-pod-monetizacion.sh", "Punto 11 · Fallo 2 (Servicios / eliminación de Pod e idempotencia).",
     "**Pods de Monetización** cae a 0 y vuelve a 1; mientras no hay Pod, los mensajes **visibles** (esperando) en la cola suben y "
     "se drenan de golpe al volver; en **Eventos consumidos** puede aparecer DUPLICADO (descartado, no cobrado); "
     "**DLQ** en 0.",
     "reemplazo en segundos e integridad con 0 cobros dobles y 0 comisiones perdidas.",
     [[(P["disponibles"](), 12, 7), (P["dlq"](), 12, 7)],
      [(P["cola_monetizacion"](), 12, 9), (P["consumidos"](), 12, 9)]]),
    ("06-fallo3", "6 · Fallo 3 · Base de datos saturada: auto-escalado en caliente",
     "./scripts/caos/fallo3-saturacion-bd.sh",
     "Punto 11 · Fallo 3 (Base de datos / saturación temporal de Aurora Serverless v2 y auto-escalado).",
     "*Simulación local de Aurora Serverless v2.* La **capacidad** sube en escalones 0,5 → 1 → 2 → 4 ACU mientras la "
     "**CPU usada** se pega a la asignada (línea punteada), con una marca naranja por escalado, y baja sola a 0,5 al "
     "terminar; el **p95 del check-out** sube durante la saturación y se recupera; **Reinicios** en 0.",
     "la capacidad sube mientras hay saturación y vuelve sola a 0,5 ACU; 0 reinicios de PostgreSQL; integridad OK.",
     [[(P["bd_acu"](), 12, 5), (P["bd_reinicios"](), 12, 5)],
      [(P["bd_cpu"](), 12, 9), (P["p95_checkout_bd"](), 12, 9)]]),
    ("10-caida-bd", "10 · Extra · Base de datos caída", "./scripts/caos/fallo3-caida-base-datos.sh",
     "Prueba extra (Base de datos / corte total transitorio): backoff exponencial, outbox y DLQ.",
     "**Errores** 5xx saltan (503: «no disponible», no errores del usuario) y vuelven a 0; **PostgreSQL** deja un hueco "
     "mientras está caído; los mensajes **visibles** (esperando) en la cola suben y se drenan al volver; **Reinicios** y **DLQ** "
     "no cambian.",
     "0 reinicios, DLQ = 0 e integridad OK (el script además dice «publicados 20 · cobrados 20»).",
     [[(P["reinicios_total"](), 12, 5), (P["dlq"](), 12, 5)],
      [(P["errores"](), 12, 9), (P["postgres"](), 12, 9)],
      [(P["cola_monetizacion"](), 24, 8)]]),
    ("07-fallo4", "7 · Fallo 4 · Recursos: OOMKilled en Monetización", "./scripts/caos/fallo4-oomkilled.sh",
     "Punto 11 · Fallo 4 (Recursos / OOMKilled).",
     "la **memoria** de Monetización sube en rampa hasta el 100 % del límite (256 Mi) y cae; **OOMKilled** pasa a 1 y "
     "**Reinicios** sube; mientras el contenedor está caído, los mensajes **visibles** (esperando) en la cola suben y se drenan al "
     "volver.",
     "OOMKilled = 1, el contenedor vuelve solo, la cola vuelve a 0, DLQ = 0 e integridad OK.",
     [[(P["oom"](), 12, 5), (P["reinicios_monetizacion"](), 12, 5)],
      [(P["mem_pct_serie"]("monetizacion"), 12, 9), (P["cola_monetizacion"](), 12, 9)]]),
    ("11-autoescalado", "11 · Auto-escalado horizontal de Monetización por la cola",
     "./scripts/caos/autoescalado-cola.sh",
     "Extra · Escalabilidad horizontal: KEDA escala el consumidor por la profundidad de la cola SQS (1 → 3 réplicas).",
     "*Requiere una vez `./scripts/deploy-autoescalado.sh`.* **Réplicas** sube en escalones 1 → 2 → 3 (marcas naranjas) "
     "y baja 3 → 2 → 1 al pasar la carga; en **Cola vs. umbral**, cada vez que la cola cruza la línea punteada llega un "
     "Pod y la cola baja; en **Consumo por Pod** aparece una curva por réplica y el TOTAL alcanza a lo que llega; la "
     "**latencia del cobro** sube mientras la cola crece y se recupera al escalar.",
     "réplicas 1 → 2 → 3 → 2 → 1 sin intervención; «cada evento se procesó exactamente una vez»; integridad OK.",
     [[(P["as_replicas_stat"](), 6, 9), (P["as_replicas"](), 18, 9)],
      [(P["as_cola"](), 12, 9), (P["as_consumo_pod"](), 12, 9)],
      [(P["cobro"](), 24, 7)]]),
    ("08-estres", "8 · Estrés · punto de quiebre 75 → 350 TPS", "./scripts/caos/estres-punto-de-quiebre.sh",
     "Punto 10.4 · Escenario 3 Estrés y Rúbrica Punto 3 (punto de quiebre).",
     "escalones en **Rate** que se «aplanan» en el máximo que el sistema alcanza; en **CPU** qué servicio llega primero "
     "al 100 % (se espera el gateway); el **p95** muy por encima de la línea roja y los **Errores** que aparecen; la "
     "recuperación al volver a 12 TPS.",
     "documentar el TPS de quiebre, el primer recurso saturado y el tiempo de recuperación, sin perder dinero.",
     [[(P["rate"](), 12, 9), (P["latencia"](), 12, 9)],
      [(P["cpu_pct"](), 12, 9), (P["errores"](), 12, 9)]]),
    ("09-resistencia", "9 · Resistencia · 12 TPS durante 1-2 h", "./scripts/k6-en-cluster.sh resistencia DURACION=1h",
     "Punto 10.4 · Escenario 4 Resistencia.",
     "**memoria** de cada servicio plana o en dientes de sierra (una rampa que sube sin parar sería una fuga); **pool** "
     "de BD estable; **p95** estable; **Reinicios** sin cambios.",
     "ninguna curva crece sostenidamente durante la hora y 0 reinicios.",
     [[(P["mem"](), 24, 9)],
      [(P["pool"](), 12, 8), (P["latencia"](), 12, 8)],
      [(P["reinicios"](), 24, 7)]]),
]

# Cómo inyectar el fallo a mano desde la terminal (demostración en vivo con la app abierta),
# en lugar del script completo que además lanza carga k6.
EN_VIVO = {
    "04-fallo1": "romper `kubectl scale deploy/verificacion-externa -n aws-local --replicas=0` · restaurar "
                 "`--replicas=1`. En la app: registrar un prestador o demandante (queda «Verificación pendiente»).",
    "05-fallo2": "romper `kubectl annotate scaledobject/monetizacion-cola -n aws-local "
                 "autoscaling.keda.sh/paused-replicas=\"0\" --overwrite` · restaurar el mismo comando terminando en "
                 "`autoscaling.keda.sh/paused-replicas-`. En la app: hacer check-out (el cobro queda «En curso»).",
    "10-caida-bd": "romper `kubectl scale statefulset/postgres -n aws-local --replicas=0` · restaurar `--replicas=1`. "
                   "En la app: cualquier acción responde «no disponible».",
    "07-fallo4": "`kubectl exec -n aws-local deploy/servicio-monetizacion -- python -c` con una fuga de 4 MiB/s "
                 "(ver docs/guia-de-pruebas.md). En la app: el check-out funciona y el cobro espera en la cola.",
}


ANOTACION_REPLICAS = {
    "datasource": DS, "enable": True, "iconColor": "orange", "name": "El HPA cambia las réplicas",
    "expr": (f"{HPA_DESEADAS} and on () (changes({HPA_DESEADAS}[15s:5s]) > 0)"), "step": "5s",
    "titleFormat": "HPA: réplicas deseadas = {{value}}",
}

ANOTACIONES = {"06-fallo3": [ANOTACION_ESCALADO], "11-autoescalado": [ANOTACION_REPLICAS]}


def tablero_prueba(archivo, titulo, comando, caso, mirar, exito, bloques):
    desde = "now-3h" if "resistencia" in archivo else "now-15m"
    return armar(f"jobbi-prueba-{archivo[:2]}", titulo, f"Guía: docs/guia-de-pruebas.md · {caso}",
                 [[(guia(comando, caso, mirar, exito,
                         "Antes de empezar: `./scripts/reiniciar-entorno.sh`. No interrumpas el script en las esperas."
                         + (f"  \n**En vivo, a mano:** {EN_VIVO[archivo]}" if archivo in EN_VIVO else "")),
                    24, 7)]] + bloques, desde=desde, etiquetas=("jobbi", "pruebas"),
                 anotaciones=ANOTACIONES.get(archivo, ()))


def tablero_indice():
    filas = "\n".join(f"| [{titulo}](/d/jobbi-prueba-{archivo[:2]}) | `{comando}` |"
                      for archivo, titulo, comando, *_ in PRUEBAS)
    md = ("### Un tablero por prueba\n\nAbre el tablero de la prueba que vas a correr: solo muestra sus paneles y "
          "explica qué mirar. La explicación completa está en `docs/guia-de-pruebas.md`.\n\n"
          "| Tablero | Comando (desde la raíz del repositorio) |\n|---|---|\n" + filas +
          "\n| [Tablero general (todos los paneles)](/d/jobbi-caso-uso) | — |\n\n"
          "**Antes de cada prueba:** `./scripts/reiniciar-entorno.sh` · **Una prueba a la vez** · "
          "**No interrumpas** los scripts en las esperas: al final verifican la integridad del dinero.")
    return armar("jobbi-prueba-00", "0 · Índice de pruebas", "Punto de partida: un tablero por prueba.",
                 [[(texto("Pruebas de carga y de fallos de JOBBI", md), 24, 16)]], etiquetas=("jobbi", "pruebas"))


if __name__ == "__main__":
    (AQUI / "jobbi-caso-uso.json").write_text(json.dumps(tablero_general(), ensure_ascii=False, indent=2))
    carpeta = AQUI / "pruebas"
    carpeta.mkdir(exist_ok=True)
    (carpeta / "00-indice.json").write_text(json.dumps(tablero_indice(), ensure_ascii=False, indent=2))
    for archivo, titulo, comando, caso, mirar, exito, bloques in PRUEBAS:
        (carpeta / f"{archivo}.json").write_text(
            json.dumps(tablero_prueba(archivo, titulo, comando, caso, mirar, exito, bloques), ensure_ascii=False, indent=2))
    print(f"Generados: jobbi-caso-uso.json y {len(PRUEBAS) + 1} tableros en {carpeta.name}/")
