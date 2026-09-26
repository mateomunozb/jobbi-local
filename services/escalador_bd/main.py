"""Escalador de la base: simula el auto-escalado de Aurora Serverless v2 en local.

Aurora Serverless v2 sube y baja la capacidad (ACU: CPU y memoria) de la base
en caliente, sin cortar conexiones, según la carga. LocalStack no lo emula, así
que este proceso hace lo mismo con el PostgreSQL del clúster:

1. Cada `INTERVALO_SEGUNDOS` lee de Prometheus la CPU y la memoria que usa el
   contenedor de PostgreSQL (cAdvisor).
2. La `Politica` decide si subir o bajar un nivel (ver politica.py).
3. Aplica el nivel con el **redimensionamiento en caliente de Kubernetes**
   (subrecurso `pods/resize`): el cgroup del contenedor cambia al instante,
   **sin reiniciar el Pod** ni cortar las conexiones.
4. Ajusta `work_mem` con `ALTER SYSTEM` + `pg_reload_conf()` para que la base
   aproveche la memoria nueva (Aurora también redimensiona su buffer al
   escalar). Al subir, primero los recursos y luego `work_mem`; al bajar, al
   revés, para no pedir memoria que ya no tiene.

Es una simulación del mecanismo, no Aurora: la capacidad vuelve al nivel mínimo
si el Pod de PostgreSQL se reinicia (el redimensionamiento no cambia la plantilla
del StatefulSet), y este proceso la vuelve a ajustar en la siguiente vuelta.

    GET /estado   nivel actual, uso medido e historial de escalados
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from prometheus_client import Counter, Gauge
from sqlalchemy import create_engine, text

from common.observabilidad import log
from common.service import crear_servicio

from .politica import BAJAR, NIVELES, SUBIR, Nivel, Politica

NAMESPACE = os.getenv("BD_NAMESPACE", "aws-local")
POD = os.getenv("BD_POD", "postgres-0")
CONTENEDOR = os.getenv("BD_CONTENEDOR", "postgres")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus.monitoring.svc.cluster.local:9090")
INTERVALO_SEGUNDOS = float(os.getenv("INTERVALO_SEGUNDOS", "5"))
ACTIVO = os.getenv("ESCALADOR_ACTIVO", "true").lower() == "true"

_SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_FILTRO = f'namespace="{NAMESPACE}", pod="{POD}", container="{CONTENEDOR}"'
# Ventana de 30 s: cAdvisor refresca cada ~10-15 s, así siempre hay dos muestras.
CONSULTA_CPU = f"sum(rate(container_cpu_usage_seconds_total{{{_FILTRO}}}[30s]))"
# max, no sum: tras un reinicio del Pod, cAdvisor mantiene unos minutos la serie
# del contenedor anterior (mismo Pod, otro id) y sumarlas duplicaría el uso.
CONSULTA_MEMORIA = f"max(container_memory_working_set_bytes{{{_FILTRO}}})"

CAPACIDAD = Gauge("jobbi_bd_capacidad_acu", "Capacidad asignada a la base, en ACU (simulación de Aurora Serverless v2)")
CPU_ASIGNADA = Gauge("jobbi_bd_cpu_asignada_nucleos", "Límite de CPU aplicado al contenedor de la base")
MEMORIA_ASIGNADA = Gauge("jobbi_bd_memoria_asignada_bytes", "Límite de memoria aplicado al contenedor de la base")
CPU_USO = Gauge("jobbi_bd_cpu_uso_nucleos", "CPU usada por la base, tal como la mide el escalador")
MEMORIA_USO = Gauge("jobbi_bd_memoria_uso_bytes", "Memoria usada por la base, tal como la mide el escalador")
WORK_MEM = Gauge("jobbi_bd_work_mem_bytes", "work_mem configurado en PostgreSQL para el nivel actual")
UMBRAL_SUBIDA = Gauge("jobbi_bd_umbral_subida_pct", "% de la CPU asignada a partir del cual la base sube de nivel")
ESCALADOS = Counter("jobbi_bd_escalados_total", "Cambios de capacidad de la base", ["direccion"])


def _nucleos(valor: str) -> float:
    return int(valor[:-1]) / 1000 if valor.endswith("m") else float(valor)


def _bytes(valor: str) -> int:
    for sufijo, factor in (("Ki", 2**10), ("Mi", 2**20), ("Gi", 2**30), ("k", 10**3), ("M", 10**6), ("G", 10**9)):
        if valor.endswith(sufijo):
            return int(float(valor[: -len(sufijo)]) * factor)
    return int(valor)


class Kubernetes:
    """Cliente mínimo de la API de Kubernetes con la cuenta de servicio del Pod."""

    def __init__(self) -> None:
        self._cliente = httpx.Client(
            base_url="https://kubernetes.default.svc", verify=str(_SA / "ca.crt"), timeout=5.0,
            headers={"Authorization": f"Bearer {(_SA / 'token').read_text()}"})
        self._ruta = f"/api/v1/namespaces/{NAMESPACE}/pods/{POD}"

    def leer_pod(self) -> dict[str, Any]:
        respuesta = self._cliente.get(self._ruta)
        respuesta.raise_for_status()
        return respuesta.json()

    def redimensionar(self, nivel: Nivel) -> None:
        # Strategic merge: el contenedor se identifica por su nombre y solo cambian sus recursos.
        parche = {"spec": {"containers": [{"name": CONTENEDOR, "resources": {
            "requests": {"cpu": f"{nivel.cpu_reserva_m}m", "memory": f"{nivel.memoria_reserva_mi}Mi"},
            "limits": {"cpu": f"{nivel.cpu_limite_m}m", "memory": f"{nivel.memoria_limite_mi}Mi"},
        }}]}}
        respuesta = self._cliente.patch(f"{self._ruta}/resize", json=parche,
                                        headers={"Content-Type": "application/strategic-merge-patch+json"})
        respuesta.raise_for_status()


def _contenedor(pod: dict[str, Any], seccion: str) -> dict[str, Any]:
    lista = pod["spec"]["containers"] if seccion == "spec" else pod["status"].get("containerStatuses", [])
    return next((c for c in lista if c["name"] == CONTENEDOR), {})


def nivel_de(pod: dict[str, Any]) -> int | None:
    """Índice del nivel que pide el Pod (su spec), o None si no coincide con ninguno."""
    limites = _contenedor(pod, "spec").get("resources", {}).get("limits", {})
    if "cpu" not in limites:
        return None
    cpu_m = round(_nucleos(limites["cpu"]) * 1000)
    return next((i for i, n in enumerate(NIVELES) if n.cpu_limite_m == cpu_m), None)


def _consultar(consulta: str) -> float | None:
    respuesta = httpx.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": consulta}, timeout=3.0)
    respuesta.raise_for_status()
    resultado = respuesta.json()["data"]["result"]
    return float(resultado[0]["value"][1]) if resultado else None


class Escalador:
    def __init__(self) -> None:
        self.k8s = Kubernetes()
        self.politica = Politica()
        self.indice = 0
        self.ultimo_uso: dict[str, float | None] = {"cpu": None, "memoria": None}
        self.ultima_decision = ""
        self.historial: deque[dict[str, Any]] = deque(maxlen=50)
        self._base = None
        UMBRAL_SUBIDA.set(self.politica.umbral_subida * 100)

    # --- PostgreSQL ---------------------------------------------------------

    def _motor(self):
        if self._base is None:
            url = (f"postgresql+psycopg://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
                   f"@{os.getenv('PGHOST', 'postgres.aws-local.svc.cluster.local')}:5432/jobbi")
            self._base = create_engine(url, pool_size=1, max_overflow=0, pool_pre_ping=True,
                                       isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
        return self._base

    def _ajustar_work_mem(self, nivel: Nivel) -> None:
        # ALTER SYSTEM no admite parámetros enlazados; el valor sale de NIVELES, no del usuario.
        try:
            with self._motor().connect() as c:
                c.execute(text(f"ALTER SYSTEM SET work_mem = '{nivel.work_mem_mb}MB'"))
                c.execute(text("SELECT pg_reload_conf()"))
            WORK_MEM.set(nivel.work_mem_mb * 2**20)
        except Exception as e:  # noqa: BLE001 — sin base, los recursos igual se ajustan
            log.warning("work_mem_no_ajustado", extra={"error": type(e).__name__, "workMemMb": nivel.work_mem_mb})

    # --- Un paso del bucle ----------------------------------------------------

    def _reflejar(self, pod: dict[str, Any]) -> None:
        """Publica lo que Kubernetes aplicó de verdad (status), no lo pedido."""
        aplicado = _contenedor(pod, "status").get("resources", {}).get("limits", {})
        if "cpu" in aplicado:
            CPU_ASIGNADA.set(_nucleos(aplicado["cpu"]))
        if "memory" in aplicado:
            MEMORIA_ASIGNADA.set(_bytes(aplicado["memory"]))
        CAPACIDAD.set(NIVELES[self.indice].acu)

    def cambiar(self, nuevo: int, motivo: str) -> None:
        anterior, nivel = self.indice, NIVELES[nuevo]
        if nuevo > anterior:
            self.k8s.redimensionar(nivel)
            self._ajustar_work_mem(nivel)
        else:
            self._ajustar_work_mem(nivel)
            self.k8s.redimensionar(nivel)
        self.indice = nuevo
        self.politica.cambio_aplicado()
        direccion = "subida" if nuevo > anterior else "bajada"
        ESCALADOS.labels(direccion).inc()
        registro = {
            "hora": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "direccion": direccion, "deAcu": NIVELES[anterior].acu, "aAcu": nivel.acu,
            "cpuAsignada": nivel.cpu_nucleos, "memoriaAsignadaMi": nivel.memoria_limite_mi,
            "workMemMb": nivel.work_mem_mb, "motivo": motivo,
            "cpuUsada": round(self.ultimo_uso["cpu"] or 0, 3),
            "memoriaUsadaMi": round((self.ultimo_uso["memoria"] or 0) / 2**20),
        }
        self.historial.append(registro)
        log.warning("bd_escalada", extra=registro)

    def paso(self) -> None:
        pod = self.k8s.leer_pod()
        pedido = nivel_de(pod)
        if pedido is None:
            # Recursos fuera de la escalera (p. ej. un manifiesto viejo): se lleva al nivel actual.
            log.warning("bd_fuera_de_nivel", extra={"aNivelAcu": NIVELES[self.indice].acu})
            self.k8s.redimensionar(NIVELES[self.indice])
            self._ajustar_work_mem(NIVELES[self.indice])
            return
        if pedido != self.indice:
            # Alguien más cambió la capacidad (o el Pod se reinició con la plantilla): se adopta.
            log.warning("bd_nivel_adoptado", extra={"deAcu": NIVELES[self.indice].acu, "aAcu": NIVELES[pedido].acu})
            self.indice = pedido
            self._ajustar_work_mem(NIVELES[pedido])
            self.politica.cambio_aplicado()
        self._reflejar(pod)

        cpu, memoria = _consultar(CONSULTA_CPU), _consultar(CONSULTA_MEMORIA)
        if cpu is None or memoria is None:
            return
        self.ultimo_uso = {"cpu": cpu, "memoria": memoria}
        CPU_USO.set(cpu)
        MEMORIA_USO.set(memoria)

        decision, motivo = self.politica.decidir(self.indice, cpu, memoria)
        self.ultima_decision = motivo
        if decision == SUBIR:
            self.cambiar(self.indice + 1, motivo)
        elif decision == BAJAR:
            self.cambiar(self.indice - 1, motivo)

    def iniciar(self) -> None:
        pod = self.k8s.leer_pod()
        self.indice = nivel_de(pod) or 0
        self._ajustar_work_mem(NIVELES[self.indice])
        log.info("escalador_activo", extra={"pod": POD, "nivelAcu": NIVELES[self.indice].acu,
                                            "cadaSegundos": INTERVALO_SEGUNDOS})
        threading.Thread(target=self._bucle, name="escalador-bd", daemon=True).start()

    def _bucle(self) -> None:
        while True:
            try:
                self.paso()
            except Exception as e:  # noqa: BLE001 — el hilo no puede morir
                log.error("escalador_error", extra={"error": f"{type(e).__name__}: {e}"})
            time.sleep(INTERVALO_SEGUNDOS)

    def estado(self) -> dict[str, Any]:
        nivel = NIVELES[self.indice]
        return {
            "activo": ACTIVO, "pod": POD, "capacidadAcu": nivel.acu, "cpuAsignada": nivel.cpu_nucleos,
            "memoriaAsignadaMi": nivel.memoria_limite_mi, "workMemMb": nivel.work_mem_mb,
            "cpuUsada": self.ultimo_uso["cpu"], "memoriaUsadaMi": (
                round(self.ultimo_uso["memoria"] / 2**20) if self.ultimo_uso["memoria"] else None),
            "ultimaDecision": self.ultima_decision,
            "niveles": [{"acu": n.acu, "cpu": n.cpu_nucleos, "memoriaMi": n.memoria_limite_mi,
                         "workMemMb": n.work_mem_mb} for n in NIVELES],
            "historial": list(self.historial),
        }


app = crear_servicio(
    nombre="escalador-bd",
    contexto="Escalador de la base (simulación de Aurora Serverless v2)",
    descripcion="Sube y baja en caliente la CPU y la memoria de PostgreSQL según su saturación.",
)
escalador: Escalador | None = None


@app.on_event("startup")
def _arrancar() -> None:
    global escalador
    if not ACTIVO:
        log.warning("escalador_deshabilitado", extra={"motivo": "ESCALADOR_ACTIVO=false"})
        return
    escalador = Escalador()
    escalador.iniciar()


@app.get("/estado", tags=["escalado"], summary="Nivel actual, uso medido e historial de escalados")
def estado() -> dict[str, Any]:
    return escalador.estado() if escalador else {"activo": False}
