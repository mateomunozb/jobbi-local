"""Consumidor del patrón Pub/Sub: worker SQS de Monetización.

Contrataciones publica CONTRATACION_COMPLETADA en el tema SNS; la cola
`monetizacion-events-queue` está suscrita a ese tema, y este worker la lee y
cobra la comisión en la billetera del prestador. Ninguno de los dos contextos
conoce al otro: si mañana otro contexto necesita el mismo evento, se suscribe
con su propia cola y el productor no cambia.

Garantías:

- **Idempotencia.** Cada evento se registra en `eventos_procesados` en la misma
  transacción que el cobro; un duplicado choca con la clave primaria y se
  descarta. SQS y el outbox entregan *al menos una vez*, así que esto no es
  opcional.
- **Sin pérdida.** El mensaje se borra de la cola solo después del commit. Si el
  procesamiento falla, SQS lo vuelve a entregar al vencer el visibility timeout
  y, tras 5 intentos, lo mueve a la DLQ (`monetizacion-events-dlq`) para
  revisarlo sin bloquear al resto.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from common import eventos
from common.enums import MedioPago
from common.observabilidad import EVENTOS_CONSUMIDOS, LATENCIA_COBRO, evento_negocio, log, usar_trace_id
from common.resiliencia import Backoff, es_error_de_base, esperar_base_disponible

from .billetera import aplicar_comision, registrar_pago_efectivo
from .comisiones import estrategia_de_cobro
from .repositorio import BandejaDeEntrada, RepositorioBilleteras

HILOS = int(os.getenv("SQS_HILOS", "2"))
# Solo para la prueba de auto-escalado: trabajo simulado por mensaje (p. ej. la
# llamada a una pasarela de pagos). Fija la capacidad de un Pod en ~1000/COSTO_MS
# mensajes/s por hilo, para saturarlo con una carga que Minikube sí aguanta.
# En 0 (lo normal) no hace nada.
COSTO_MS = float(os.getenv("SQS_COSTO_MS", "0"))

ESTADO: dict[str, Any] = {
    "activo": False,
    "conectado": False,
    "cola": eventos.QUEUE_NAME,
    "dlq": eventos.DLQ_NAME,
    "hilos": HILOS,
    "costoSimuladoMs": COSTO_MS,
    "recibidos": 0,
    "procesados": 0,
    "duplicados": 0,
    "errores": 0,
    "porResultado": Counter(),
    "ultimoError": None,
    "ultimoMensajeEn": None,
}
_candado = threading.Lock()
_urls: dict[str, str] = {}


def _contar(clave: str, resultado: str | None = None) -> None:
    with _candado:
        ESTADO[clave] += 1
        if resultado:
            ESTADO["porResultado"][resultado] += 1


def _desenvolver(cuerpo_sqs: str) -> tuple[dict[str, Any], str | None]:
    """Devuelve (evento, MessageId de SNS). SNS envuelve el mensaje en 'Message'."""
    cuerpo = json.loads(cuerpo_sqs)
    if isinstance(cuerpo, dict) and "Message" in cuerpo and "TopicArn" in cuerpo:
        try:
            return json.loads(cuerpo["Message"]), cuerpo.get("MessageId")
        except (TypeError, ValueError):
            return {"crudo": cuerpo["Message"]}, cuerpo.get("MessageId")
    return cuerpo, None


def _fecha(valor: Any) -> datetime | None:
    """Fecha del evento en hora local sin zona, como las que guarda el servicio.

    El outbox escribe hora local; otros productores (k6, la CLI) mandan UTC con
    zona. Quitarle la zona sin convertir descuadraría la latencia en horas.
    """
    try:
        fecha = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return fecha.astimezone().replace(tzinfo=None) if fecha.tzinfo else fecha


def _monto_comision(evento: dict[str, Any]) -> float:
    """Comisión a cobrar, calculada con la estrategia que corresponde al evento.

    Sin valor acordado ni estrategia (eventos de productores antiguos o de la
    prueba de carga) se cobra el monto que trae el evento, como antes.
    """
    estrategia = estrategia_de_cobro(evento)
    valor = evento.get("valorAcordado")
    if estrategia is not None and valor is not None:
        return estrategia.calcular(float(valor))
    return float(evento.get("montoComision", evento.get("monto", 0.0)))


def procesar_mensaje(sesiones: sessionmaker[Session], cuerpo_sqs: str, mensaje_sqs_id: str) -> str:
    """Procesa un mensaje y devuelve el resultado. Lanza si hay que reintentarlo."""
    evento, sns_id = _desenvolver(cuerpo_sqs)
    # Retoma la traza del check-out que originó el evento.
    usar_trace_id(evento.get("traceId"))
    resultado = _procesar(sesiones, evento, sns_id, mensaje_sqs_id)
    EVENTOS_CONSUMIDOS.labels("monetizacion", resultado).inc()
    if resultado == "DUPLICADO":
        evento_negocio("evento_duplicado", "Evento ya procesado: se descarta sin cobrar",
                       eventoId=evento.get("eventoId") or sns_id, contratacionId=evento.get("contratacionId"))
    return resultado


def _procesar(sesiones: sessionmaker[Session], evento: dict[str, Any], sns_id: str | None,
              mensaje_sqs_id: str) -> str:
    # El eventoId lo pone el outbox. Los mensajes publicados a mano o por k6
    # contra SNS no lo traen: para esos, el MessageId de SNS es igual de único.
    evento_id = str(evento.get("eventoId") or sns_id or mensaje_sqs_id)
    tipo = str(evento.get("tipo") or evento.get("evento") or eventos.CONTRATACION_COMPLETADA)
    contratacion_id = evento.get("contratacionId")
    prestador_id = evento.get("prestadorId")
    ocurrido = _fecha(evento.get("ocurridoEn") or evento.get("timestamp"))

    bloqueo = None
    # Unidad de trabajo: bandeja de entrada, cargo, pago y evento del outbox se
    # confirman en una sola transacción, o ninguno.
    with sesiones() as s:
        bandeja, billeteras = BandejaDeEntrada(s), RepositorioBilleteras(s)
        if bandeja.ya_procesado(evento_id):
            return "DUPLICADO"

        if tipo == eventos.CONTRATACION_COMPLETADA and contratacion_id and prestador_id:
            monto = _monto_comision(evento)
            if evento.get("medioPago", MedioPago.EFECTIVO.value) == MedioPago.EFECTIVO.value:
                cobro = aplicar_comision(billeteras, prestador_id, contratacion_id, monto)
                bloqueo = cobro.bloqueo
                if evento.get("valorAcordado") is not None:
                    registrar_pago_efectivo(billeteras, contratacion_id, float(evento["valorAcordado"]),
                                            (ocurrido or datetime.now()).date())
                resultado = "YA_COBRADA" if cobro.yaCobrada else "COMISION_COBRADA"
            else:
                # Con pago por plataforma la comisión se retiene del pago, que
                # aún no está implementado: se registra el evento sin cobrar.
                resultado = "SIN_COBRO_MEDIO_PLATAFORMA"
        else:
            # Evento sin contratación real (p. ej. la prueba de carga que
            # publica directo en SNS): se registra para poder contarlo.
            monto = float(evento.get("monto", 0.0))
            contratacion_id = contratacion_id or evento.get("servicio_id")
            resultado = "REGISTRADO_SIN_CONTRATACION"

        ahora = datetime.now()
        bandeja.registrar(
            evento_id=evento_id, tipo=tipo,
            contratacion_id=str(contratacion_id) if contratacion_id else None,
            prestador_id=prestador_id, monto=monto, resultado=resultado,
            mensaje_sqs_id=mensaje_sqs_id, ocurrido=ocurrido, procesado=ahora,
        )
        try:
            s.commit()
        except IntegrityError:
            # Otro hilo procesó el mismo evento a la vez y ganó: es un duplicado.
            s.rollback()
            return "DUPLICADO"

    # Los hechos de negocio se registran después del commit: solo lo que quedó.
    if resultado == "COMISION_COBRADA":
        evento_negocio("comision_aplicada", "Comisión cargada a la billetera del prestador",
                       contratacionId=contratacion_id, prestadorId=prestador_id, monto=monto,
                       latenciaMs=round((ahora - ocurrido).total_seconds() * 1000, 1) if ocurrido else None)
        if ocurrido:
            LATENCIA_COBRO.observe(max((ahora - ocurrido).total_seconds(), 0))
    if bloqueo:
        evento_negocio("billetera_bloqueada", "La billetera alcanzó el umbral y quedó bloqueada",
                       logging.WARNING, **bloqueo)
    return resultado


def _url_de(sqs, nombre: str) -> str:
    if nombre not in _urls:
        _urls[nombre] = sqs.get_queue_url(QueueName=nombre)["QueueUrl"]
    return _urls[nombre]


def estado_colas() -> dict[str, Any]:
    """Profundidad aproximada de la cola principal y de la DLQ."""
    sqs = eventos.cliente("sqs")
    colas = {}
    for clave, nombre in (("principal", eventos.QUEUE_NAME), ("dlq", eventos.DLQ_NAME)):
        try:
            atributos = sqs.get_queue_attributes(
                QueueUrl=_url_de(sqs, nombre),
                AttributeNames=["ApproximateNumberOfMessages",
                                "ApproximateNumberOfMessagesNotVisible"],
            )["Attributes"]
            colas[clave] = {
                "nombre": nombre,
                "visibles": int(atributos.get("ApproximateNumberOfMessages", 0)),
                "enVuelo": int(atributos.get("ApproximateNumberOfMessagesNotVisible", 0)),
            }
        except Exception as e:  # noqa: BLE001
            _urls.pop(nombre, None)
            colas[clave] = {"nombre": nombre, "error": str(e)}
    return colas


def _bucle(sesiones: sessionmaker[Session]) -> None:
    sqs = eventos.cliente("sqs")
    backoff_sqs = Backoff(maximo=30.0)
    while True:
        base_caida = False
        try:
            url = _url_de(sqs, eventos.QUEUE_NAME)
            if not ESTADO["conectado"]:
                log.info("sqs_conectado", extra={"cola": url})
            ESTADO["conectado"] = True
            backoff_sqs.reiniciar()

            # Long polling: la llamada espera hasta 10 s a que haya mensajes, en
            # vez de preguntar en vacío varias veces por segundo.
            respuesta = sqs.receive_message(
                QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=10,
            )
            listos = []
            for mensaje in respuesta.get("Messages", []):
                _contar("recibidos")
                if COSTO_MS:
                    time.sleep(COSTO_MS / 1000)
                try:
                    resultado = procesar_mensaje(sesiones, mensaje["Body"], mensaje["MessageId"])
                except Exception as e:  # noqa: BLE001
                    # No se borra: SQS lo reintentará y, si insiste, irá a la DLQ.
                    _contar("errores")
                    ESTADO["ultimoError"] = f"{type(e).__name__}: {e}"
                    EVENTOS_CONSUMIDOS.labels("monetizacion", "ERROR").inc()
                    base_caida = es_error_de_base(e)
                    log.error("cobro_fallido", extra={
                        "business_event": "cobro_fallido", "mensajeSqsId": mensaje["MessageId"],
                        "error": ESTADO["ultimoError"],
                        "accion": "pausa con backoff hasta que vuelva la base" if base_caida
                                  else "SQS lo reentregará (DLQ tras 5)"})
                    if base_caida:
                        # El resto del lote fallaría igual: se deja sin tocar y
                        # SQS lo devolverá a la cola al vencer su visibilidad.
                        break
                    continue
                _contar("duplicados" if resultado == "DUPLICADO" else "procesados", resultado)
                ESTADO["ultimoMensajeEn"] = datetime.now()
                listos.append({"Id": mensaje["MessageId"], "ReceiptHandle": mensaje["ReceiptHandle"]})

            if listos:
                sqs.delete_message_batch(QueueUrl=url, Entries=listos)
        except Exception as e:  # noqa: BLE001 — LocalStack caído o reiniciado
            espera = backoff_sqs.siguiente()
            log.warning("sqs_no_disponible" if ESTADO["conectado"] else "sqs_esperando_cola",
                        extra={"endpoint": eventos.AWS_ENDPOINT_URL, "error": str(e),
                               "reintentoEnSegundos": round(espera, 1)})
            ESTADO["conectado"] = False
            ESTADO["ultimoError"] = str(e)
            _urls.clear()
            time.sleep(espera)
            continue

        if base_caida:
            # Con la base caída no se reciben más mensajes: cada recepción
            # sumaría un intento en SQS y, tras 5, una comisión válida acabaría
            # en la DLQ. Se espera con backoff exponencial a que vuelva.
            esperar_base_disponible(sesiones, "sqs-worker-monetizacion")


def iniciar_worker(sesiones: sessionmaker[Session]) -> bool:
    """Arranca los hilos consumidores. Devuelve si quedaron activos."""
    if not eventos.activado("SQS_ENABLED"):
        log.warning("sqs_worker_deshabilitado", extra={"motivo": "SQS_ENABLED=false"})
        return False
    for i in range(HILOS):
        threading.Thread(target=_bucle, args=(sesiones,), name=f"sqs-worker-{i}", daemon=True).start()
    ESTADO["activo"] = True
    return True
