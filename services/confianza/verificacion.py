"""Verificación de prestadores con el aliado externo (RN-01).

Cuándo se consulta al aliado:

1. **Al registrarse el prestador.** El gateway pide la verificación y el
   prestador queda APROBADA o RECHAZADA al instante; si el aliado está caído o
   el circuito abierto, queda **PENDIENTE**.
2. **En segundo plano**, cada VERIFICACION_REINTENTO_SEGUNDOS, el reverificador
   reintenta a los PENDIENTE y renueva los veredictos que cumplen
   VERIFICACION_VIGENCIA_DIAS. Si el circuito está abierto, ni lo intenta.

Nada más llama al aliado: aceptar acuerdos, cobrar o buscar leen el estado ya
guardado. Un PENDIENTE o RECHAZADO no aparece en la búsqueda ni cierra acuerdos;
un APROBADO cuyo veredicto venció conserva su estado hasta que se renueve (una
caída del aliado no castiga a quien ya estaba aprobado).

Confianza es la dueña del veredicto; Identidad guarda la copia que muestra el
perfil y filtra la búsqueda. Tras cada veredicto se le publica por HTTP, y si
no responde, el reverificador lo vuelve a intentar (`sincronizado`).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from common import eventos
from common.db import nuevo_id
from common.enums import EstadoVerificacion
from common.observabilidad import cabeceras_de_traza, evento_negocio, log
from common.resiliencia import ABIERTO, CircuitoAbierto, es_error_de_base, esperar_base_disponible

from . import adaptador_verificacion as adaptador
from .tablas import AliadoVerificacionFila, SujetoVerificacionFila, VerificacionIdentidadFila

PENDIENTE, APROBADA, RECHAZADA = (EstadoVerificacion.PENDIENTE.value, EstadoVerificacion.APROBADA.value,
                                  EstadoVerificacion.RECHAZADA.value)

# Cuánto dura un veredicto del aliado: dentro de ese plazo no se le vuelve a
# consultar (cada consulta cuesta dinero).
VIGENCIA_DIAS = int(os.getenv("VERIFICACION_VIGENCIA_DIAS", "30"))
REINTENTO_SEGUNDOS = float(os.getenv("VERIFICACION_REINTENTO_SEGUNDOS", "15"))
IDENTIDAD_URL = os.getenv("SERVICIO_IDENTIDAD_URL", "http://identidad.aws-local.svc.cluster.local:8001")

_ALIADO_SIMULADO = "Truora (simulado)"

# Origen de la respuesta de verificar().
ALIADO, VIGENTE, CIRCUITO_ABIERTO, FALLO_DEL_ALIADO = "ALIADO", "VIGENTE", "CIRCUITO_ABIERTO", "FALLO_DEL_ALIADO"


def _aliado(s: Session) -> AliadoVerificacionFila:
    aliado = s.scalars(select(AliadoVerificacionFila).where(
        AliadoVerificacionFila.nombre == _ALIADO_SIMULADO)).first()
    if aliado is None:
        aliado = AliadoVerificacionFila(id=nuevo_id(), nombre=_ALIADO_SIMULADO,
                                        costoPorConsulta=3500.0, slaHoras=48)
        s.add(aliado)
        s.flush()
    return aliado


def _guardar_detalle(s: Session, prestador_id: str, resultado: adaptador.ResultadoVerificacion) -> None:
    """Una fila por (prestador, tipo) con el último veredicto y su costo.

    Una consulta al aliado trae los dos tipos a la vez y se cobra una sola vez:
    el costo se reparte entre las filas, sin duplicarlo.
    """
    aliado = _aliado(s)
    hoy = date.today()
    costo_por_tipo = aliado.costoPorConsulta / len(resultado.estados)
    for tipo, estado in resultado.estados.items():
        fila = s.scalars(select(VerificacionIdentidadFila).where(
            VerificacionIdentidadFila.prestadorId == prestador_id,
            VerificacionIdentidadFila.tipoVerificacion == tipo,
        )).first()
        if fila is None:
            fila = VerificacionIdentidadFila(
                id=nuevo_id(), prestadorId=prestador_id, aliadoVerificacionId=aliado.id,
                tipoVerificacion=tipo, fechaSolicitud=hoy, costo=0.0, resultadoDetalle="", estado=estado.value)
            s.add(fila)
        fila.estado = estado.value
        fila.fechaResultado = hoy if estado != EstadoVerificacion.PENDIENTE else None
        fila.resultadoDetalle = f"{resultado.referenciaExterna} {resultado.detalle}"
        fila.costo += costo_por_tipo


def estado_de(resultado: adaptador.ResultadoVerificacion) -> str:
    """Veredicto global: APROBADA solo con identidad y antecedentes aprobados (RN-01)."""
    if adaptador.insignia_vigente(resultado.estados):
        return APROBADA
    if EstadoVerificacion.RECHAZADA in resultado.estados.values():
        return RECHAZADA
    return PENDIENTE


def vigente(sujeto: SujetoVerificacionFila) -> bool:
    return (sujeto.estado != PENDIENTE and sujeto.fechaVeredicto is not None
            and sujeto.fechaVeredicto > date.today() - timedelta(days=VIGENCIA_DIAS))


def verificar(s: Session, sujeto: SujetoVerificacionFila) -> str:
    """Consulta al aliado si hace falta y actualiza el sujeto. No hace commit. Devuelve el origen.

    Si el aliado falla, el sujeto conserva su estado: un PENDIENTE sigue
    PENDIENTE y un APROBADO vencido sigue APROBADO hasta poder renovarlo.
    """
    if vigente(sujeto):
        return VIGENTE
    sujeto.intentos += 1
    sujeto.ultimoIntento = datetime.now()
    try:
        resultado = adaptador.verificar(sujeto.prestadorId, sujeto.documento)
    except CircuitoAbierto:
        return CIRCUITO_ABIERTO
    except httpx.HTTPError as e:
        log.warning("verificacion_degradada", extra={"prestadorId": sujeto.prestadorId, "error": type(e).__name__})
        return FALLO_DEL_ALIADO

    _guardar_detalle(s, sujeto.prestadorId, resultado)
    nuevo = estado_de(resultado)
    if nuevo != sujeto.estado:
        sujeto.sincronizado = False
    sujeto.estado = nuevo
    if nuevo != PENDIENTE:
        sujeto.fechaVeredicto = date.today()
    evento_negocio("prestador_verificado", "El aliado dio su veredicto sobre el prestador",
                   prestadorId=sujeto.prestadorId, estado=nuevo)
    return ALIADO


def publicar_en_identidad(sujeto: SujetoVerificacionFila) -> bool:
    """Le pasa el veredicto a Identidad (insignia y búsqueda). Devuelve si lo aceptó."""
    try:
        respuesta = httpx.post(f"{IDENTIDAD_URL}/prestadores/{sujeto.prestadorId}/verificacion",
                               json={"estado": sujeto.estado}, timeout=3.0, headers=cabeceras_de_traza())
        respuesta.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("identidad_sin_veredicto", extra={"prestadorId": sujeto.prestadorId, "error": type(e).__name__})
        return False
    sujeto.sincronizado = True
    return True


def solicitar(s: Session, prestador_id: str, documento: str | None) -> tuple[SujetoVerificacionFila, str]:
    """Registra (o actualiza) la solicitud y verifica. Hace commit. Devuelve (sujeto, origen)."""
    sujeto = s.get(SujetoVerificacionFila, prestador_id)
    if sujeto is None:
        if not documento:
            raise ValueError("Se requiere el documento para la primera verificación")
        sujeto = SujetoVerificacionFila(prestadorId=prestador_id, documento=documento, estado=PENDIENTE,
                                        sincronizado=True, intentos=0, creadoEn=datetime.now())
        s.add(sujeto)
    elif documento:
        sujeto.documento = documento
    origen = verificar(s, sujeto)
    s.commit()
    if not sujeto.sincronizado and publicar_en_identidad(sujeto):
        s.commit()
    return sujeto, origen


def resumen(sujeto: SujetoVerificacionFila | None, origen: str | None = None) -> dict[str, Any]:
    estado = sujeto.estado if sujeto else "SIN_SOLICITUD"
    return {
        "prestadorId": sujeto.prestadorId if sujeto else None,
        "estado": estado,
        "insigniaVigente": estado == APROBADA,
        "fechaVeredicto": sujeto.fechaVeredicto if sujeto else None,
        "intentos": sujeto.intentos if sujeto else 0,
        "origen": origen,
        "degradado": origen in (CIRCUITO_ABIERTO, FALLO_DEL_ALIADO),
        "circuito": adaptador.circuito.estado,
    }


# --- Reverificador de fondo ---------------------------------------------------------

def reverificar(sesiones: sessionmaker[Session], limite: int = 25) -> int:
    """Una pasada: pendientes, vencidos y sin sincronizar. Devuelve cuántos atendió."""
    vencido = date.today() - timedelta(days=VIGENCIA_DIAS)
    with sesiones() as s:
        sujetos = s.scalars(select(SujetoVerificacionFila).where(or_(
            SujetoVerificacionFila.estado == PENDIENTE,
            SujetoVerificacionFila.sincronizado.is_(False),
            SujetoVerificacionFila.fechaVeredicto <= vencido,
        )).order_by(SujetoVerificacionFila.creadoEn).limit(limite)).all()
        for sujeto in sujetos:
            # Con el circuito abierto no se llama al aliado; sí se sincroniza lo pendiente.
            if adaptador.circuito.estado != ABIERTO:
                verificar(s, sujeto)
                s.commit()
            if not sujeto.sincronizado and sujeto.estado != PENDIENTE and publicar_en_identidad(sujeto):
                s.commit()
        return len(sujetos)


def _bucle(sesiones: sessionmaker[Session]) -> None:
    log.info("reverificador_activo", extra={"cadaSegundos": REINTENTO_SEGUNDOS, "vigenciaDias": VIGENCIA_DIAS})
    while True:
        time.sleep(REINTENTO_SEGUNDOS)
        try:
            reverificar(sesiones)
        except Exception as e:  # noqa: BLE001 — el hilo no puede morir
            if es_error_de_base(e):
                esperar_base_disponible(sesiones, "reverificador")
            else:
                log.error("reverificador_error", extra={"error": f"{type(e).__name__}: {e}"})


def iniciar_reverificador(sesiones: sessionmaker[Session]) -> bool:
    if not eventos.activado("VERIFICACION_REINTENTOS"):
        log.warning("reverificador_deshabilitado", extra={"motivo": "VERIFICACION_REINTENTOS=false"})
        return False
    threading.Thread(target=_bucle, args=(sesiones,), name="reverificador", daemon=True).start()
    return True
