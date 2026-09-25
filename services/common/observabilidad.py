"""Observabilidad compartida: métricas para Prometheus, logs JSON y traceId.

Todo servicio creado con `crear_servicio` obtiene, sin escribir nada más:

- **`/metrics`** en el formato de Prometheus, con el histograma
  `jobbi_http_request_duration_seconds{service,method,route,status}`. De él
  salen las tres dimensiones RED: tasa (`_count`), errores (`status=~"5.."`) y
  duración (`_bucket`). La ruta es la plantilla (`/contrataciones/{id}/check-out`)
  y no la URL real, para que la cardinalidad no crezca con cada id.
- **Logs JSON** en stdout (`timestamp, level, service, traceId, message` y, en
  los hechos de negocio, `business_event`). Nunca se registran documentos,
  correos ni datos bancarios (OWASP A04): solo ids y montos.
- **traceId**: se toma de la cabecera `X-Trace-Id` o se crea, viaja en la
  respuesta y en las llamadas salientes, y el outbox lo copia en el payload de
  cada evento. Así un check-out y el cobro que desencadena en otro contexto se
  encuentran con el mismo id.

Las métricas de negocio no son contadores en memoria: cada servicio registra
un colector (`registrar_metricas`) que las calcula en la base en el momento del
scrape. La base es la fuente de verdad y el valor sobrevive a reinicios del Pod.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily

CABECERA_TRAZA = "X-Trace-Id"

_traza: contextvars.ContextVar[str | None] = contextvars.ContextVar("traceId", default=None)
_servicio = os.getenv("SERVICE_MODULE", "jobbi")

# Rutas que no son tráfico del negocio: las probes de Kubernetes y el propio scrape.
_RUTAS_OPERATIVAS = {"/health", "/metrics"}

# Cubre desde respuestas de pocos ms hasta 60 s (el tiempo máximo que espera
# el cliente de k6); los límites 0.3 y 0.4 coinciden con los SLO de p95 del caso
# de uso. Los rangos de 20, 30 y 60 s existen para medir la latencia real con el
# gateway saturado: sin ellos, todo lo que pasa de 10 s se vería como "10 s".
DURACION_HTTP = Histogram(
    "jobbi_http_request_duration_seconds",
    "Duración de las peticiones HTTP atendidas, por servicio, ruta y código",
    ["service", "method", "route", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.4, 0.6, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0),
)

EVENTOS_CONSUMIDOS = Counter(
    "jobbi_eventos_consumidos_total",
    "Mensajes de SQS procesados por un consumidor, por resultado",
    ["consumidor", "resultado"],
)

EVENTOS_PUBLICADOS = Counter(
    "jobbi_eventos_publicados_total",
    "Eventos que el relay del outbox publicó en SNS, por tema",
    ["tema"],
)

LATENCIA_COBRO = Histogram(
    "jobbi_cobro_latencia_seconds",
    "Desde el check-out (ocurridoEn del evento) hasta que la comisión quedó en la billetera",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 300.0),
)


# --- traceId ---------------------------------------------------------------

def trace_id() -> str | None:
    return _traza.get()


def usar_trace_id(valor: str | None) -> str:
    """Fija el traceId del contexto actual (petición o mensaje) y lo devuelve."""
    valor = valor or uuid.uuid4().hex
    _traza.set(valor)
    return valor


def cabeceras_de_traza() -> dict[str, str]:
    """Cabeceras para propagar el traceId en una llamada HTTP saliente."""
    actual = trace_id()
    return {CABECERA_TRAZA: actual} if actual else {}


# --- Logs JSON -------------------------------------------------------------

class FormatoJson(logging.Formatter):
    _CAMPOS_ESTANDAR = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

    def format(self, registro: logging.LogRecord) -> str:
        linea: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(registro.created, timezone.utc).isoformat(),
            "level": registro.levelname,
            "service": _servicio,
            "traceId": trace_id(),
            "message": registro.getMessage(),
        }
        # Lo que se pasó en `extra=` (business_event, ids, montos) va tal cual.
        for clave, valor in vars(registro).items():
            if clave not in self._CAMPOS_ESTANDAR and not clave.startswith("_"):
                linea[clave] = valor
        if registro.exc_info:
            linea["error"] = self.formatException(registro.exc_info)
        return json.dumps(linea, default=str, ensure_ascii=False)


def _configurar_logs() -> logging.Logger:
    raiz = logging.getLogger("jobbi")
    if not raiz.handlers:
        salida = logging.StreamHandler(sys.stdout)
        salida.setFormatter(FormatoJson())
        raiz.addHandler(salida)
        raiz.setLevel(os.getenv("LOG_LEVEL", "INFO"))
        raiz.propagate = False
    return raiz


log = _configurar_logs()


def evento_negocio(nombre: str, mensaje: str, nivel: int = logging.INFO, **campos: Any) -> None:
    """Registra un hecho de negocio (comision_aplicada, billetera_bloqueada, …)."""
    log.log(nivel, mensaje, extra={"business_event": nombre, **campos})


# --- Middleware HTTP -------------------------------------------------------

def instrumentar(app) -> None:
    """Añade traceId, histograma RED, log de acceso JSON y `/metrics` a la app."""

    @app.middleware("http")
    async def _observar(request, call_next):
        usar_trace_id(request.headers.get(CABECERA_TRAZA))
        inicio = time.perf_counter()
        estado = 500
        try:
            respuesta = await call_next(request)
            estado = respuesta.status_code
            respuesta.headers[CABECERA_TRAZA] = trace_id() or ""
            return respuesta
        finally:
            ruta_app = request.scope.get("route")
            ruta = getattr(ruta_app, "path", None) or "sin_ruta"
            if ruta not in _RUTAS_OPERATIVAS:
                duracion = time.perf_counter() - inicio
                DURACION_HTTP.labels(_servicio, request.method, ruta, str(estado)).observe(duracion)
                log.log(logging.ERROR if estado >= 500 else logging.INFO, "http_request", extra={
                    "method": request.method, "route": ruta, "status": estado,
                    "durationMs": round(duracion * 1000, 1),
                })

    from fastapi import Response

    @app.get("/metrics", tags=["operación"], summary="Métricas en formato Prometheus",
             include_in_schema=False)
    def metricas() -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


# --- Métricas de negocio calculadas en la base ------------------------------

# (nombre, ayuda, etiquetas, valor)
Muestra = tuple[str, str, dict[str, str], float]


class _ColectorDeNegocio:
    def __init__(self, calcular: Callable[[], Iterable[Muestra]]) -> None:
        self._calcular = calcular

    def describe(self):
        # Sin descripción previa: así registrarlo no dispara una consulta a la
        # base al importar el servicio.
        return []

    def collect(self):
        try:
            muestras = list(self._calcular())
        except Exception as e:  # noqa: BLE001 — un scrape fallido no debe tumbar /metrics
            log.warning("metricas_de_negocio_no_disponibles", extra={"error": str(e)})
            return
        familias: dict[str, GaugeMetricFamily] = {}
        for nombre, ayuda, etiquetas, valor in muestras:
            if nombre not in familias:
                familias[nombre] = GaugeMetricFamily(nombre, ayuda, labels=list(etiquetas))
            familias[nombre].add_metric(list(etiquetas.values()), float(valor))
        yield from familias.values()


def registrar_metricas(calcular: Callable[[], Iterable[Muestra]]) -> None:
    """Registra una función que produce gauges de negocio en cada scrape."""
    REGISTRY.register(_ColectorDeNegocio(calcular))


def muestras_de_colas(colas: dict[str, str]) -> list[Muestra]:
    """Profundidad de colas SQS como gauges `jobbi_sqs_mensajes{cola,estado}`.

    Cliente propio con timeouts cortos: si LocalStack no responde, el scrape
    no se queda colgado; esas series simplemente faltan en esa pasada.

    Cada consumidor exporta sus colas y el gateway exporta todas: así la cola
    se sigue viendo cuando el Pod de su consumidor está caído (Fallo 2), que es
    justo cuando se llena. Por eso las consultas usan `max by (cola)`, no `sum`.
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError

    from . import eventos

    sqs = boto3.client(
        "sqs", endpoint_url=eventos.AWS_ENDPOINT_URL, region_name=eventos.AWS_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
        config=Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 1}),
    )
    muestras: list[Muestra] = []
    for nombre in colas.values():
        try:
            url = sqs.get_queue_url(QueueName=nombre)["QueueUrl"]
            atributos = sqs.get_queue_attributes(
                QueueUrl=url,
                AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
            )["Attributes"]
        except (EndpointConnectionError, ConnectTimeoutError):
            # LocalStack no responde: las demás colas tampoco, y esperar su
            # timeout una por una alargaría el scrape de todo /metrics.
            break
        except Exception:  # noqa: BLE001
            continue
        for estado, atributo in (("visibles", "ApproximateNumberOfMessages"),
                                 ("en_vuelo", "ApproximateNumberOfMessagesNotVisible")):
            muestras.append(("jobbi_sqs_mensajes", "Mensajes en una cola SQS (las *-dlq son la DLQ)",
                             {"cola": nombre, "estado": estado}, int(atributos.get(atributo, 0))))
    return muestras
