"""Contexto delimitado: Monetización.

Dueño de pagos, suscripciones Pro, billeteras y movimientos, en la base
`jobbi_monetizacion`. Es el consumidor del evento CONTRATACION_COMPLETADA: el
worker SQS (`sqs_worker.py`) cobra la comisión de cada servicio completado.
Conserva los endpoints /cobros y /cobrar del simulador original.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from common.db import condiciones, inicializar, nuevo_id, paginar_consulta
from common.enums import MedioPago, PlanPrestador
from common.service import crear_servicio, resumen_latencias

from .billetera import UMBRAL_BLOQUEO, aplicar_comision
from .models import BilleteraPrestador, EventoProcesado, MovimientoBilletera, Pago, SuscripcionPro
from .sqs_worker import ESTADO, estado_colas, iniciar_worker
from .tablas import (
    BilleteraPrestadorFila,
    EventoProcesadoFila,
    MovimientoBilleteraFila,
    PagoFila,
    SuscripcionProFila,
)

app = crear_servicio(
    nombre="monetizacion",
    contexto="Monetización",
    descripcion=(
        "Pagos por contratación, suscripciones Pro, billetera del prestador y "
        "sus movimientos. Consume el evento CONTRATACION_COMPLETADA vía SNS/SQS."
    ),
)


Sesion: sessionmaker[Session] = inicializar("monetizacion")

_worker_activo = False

# La tarifa de la plataforma es una regla de este contexto, y este es el único
# lugar donde vive. Quien la necesite (el gateway, al cerrar un acuerdo) la pide
# por /planes en vez de llevar el número escrito.
PLANES = {
    PlanPrestador.FREE.value: {"porcentajeComision": 0.18, "valorMensual": 0.0},
    PlanPrestador.PRO.value: {"porcentajeComision": 0.12, "valorMensual": 39900.0},
}


def sesion() -> Session:
    with Sesion() as s:
        yield s


@app.on_event("startup")
def _arrancar_worker() -> None:
    global _worker_activo
    _worker_activo = iniciar_worker(Sesion)


# --- Integración Pub/Sub ---------------------------------------------------
def _cobro_legado(fila: EventoProcesadoFila) -> dict:
    """Forma de /cobros del simulador original, más el resultado del cobro."""
    return {
        "id": fila.eventoId,
        "evento": fila.tipo,
        "monto": fila.monto,
        "servicio_id": fila.contratacionId or "N/A",
        "tipo": fila.origen,
        "resultado": fila.resultado,
        "fecha": fila.fechaProcesado,
        "latenciaMs": fila.latenciaMs,
    }


@app.get("/cobros", tags=["pub/sub"], summary="Eventos procesados por el worker SQS")
def listar_cobros(
    limite: int = Query(50, ge=1, le=500, description="Cuántos de los más recientes devolver"),
    s: Session = Depends(sesion),
):
    total = s.scalar(select(func.count()).select_from(EventoProcesadoFila)) or 0
    filas = s.scalars(
        select(EventoProcesadoFila).order_by(EventoProcesadoFila.fechaProcesado.desc()).limit(limite)
    ).all()
    return {"total": total, "cobros": [_cobro_legado(f) for f in filas]}


@app.get("/cobros/estado-worker", tags=["pub/sub"],
         summary="Estado del consumidor SQS y profundidad de las colas")
def estado_worker(s: Session = Depends(sesion)):
    por_resultado = dict(s.execute(
        select(EventoProcesadoFila.resultado, func.count()).group_by(EventoProcesadoFila.resultado)
    ).all())
    latencias = list(s.scalars(
        select(EventoProcesadoFila.latenciaMs)
        # Solo eventos de contrataciones reales: los de la prueba de carga del
        # broker no pasan por el outbox y medirían otra cosa.
        .where(EventoProcesadoFila.latenciaMs.is_not(None),
               EventoProcesadoFila.resultado.in_(("COMISION_COBRADA", "YA_COBRADA")))
        .order_by(EventoProcesadoFila.fechaProcesado.desc())
        .limit(200)
    ).all())
    return {
        "workerActivo": _worker_activo,
        "conectado": ESTADO["conectado"],
        "hilos": ESTADO["hilos"],
        # Totales persistidos (sobreviven a reinicios del Pod).
        "cobrosProcesados": sum(por_resultado.values()),
        "porResultado": por_resultado,
        # Contadores de este proceso desde que arrancó.
        "desdeArranque": {
            "recibidos": ESTADO["recibidos"],
            "procesados": ESTADO["procesados"],
            "duplicadosDescartados": ESTADO["duplicados"],
            "errores": ESTADO["errores"],
        },
        "latenciaExtremoAExtremoMs": resumen_latencias(latencias),
        "ultimoMensajeEn": ESTADO["ultimoMensajeEn"],
        "ultimoError": ESTADO["ultimoError"],
        "colas": estado_colas() if _worker_activo else None,
    }


@app.get("/cobros/contratacion/{contratacion_id}", tags=["pub/sub"],
         summary="¿Ya se cobró la comisión de esta contratación?")
def cobro_de_contratacion(contratacion_id: str, s: Session = Depends(sesion)):
    evento = s.scalars(select(EventoProcesadoFila).where(
        EventoProcesadoFila.contratacionId == contratacion_id,
    ).order_by(EventoProcesadoFila.fechaProcesado)).first()
    movimiento = s.scalars(select(MovimientoBilleteraFila).where(
        MovimientoBilleteraFila.contratacionId == contratacion_id,
        MovimientoBilleteraFila.tipo == "COMISION",
    )).first()
    billetera = s.get(BilleteraPrestadorFila, movimiento.billeteraId) if movimiento else None
    return {
        "contratacionId": contratacion_id,
        "cobrado": movimiento is not None,
        "evento": EventoProcesado.model_validate(evento) if evento else None,
        "movimiento": MovimientoBilletera.model_validate(movimiento) if movimiento else None,
        "billetera": BilleteraPrestador.model_validate(billetera) if billetera else None,
    }


@app.post("/cobrar", tags=["pub/sub"], summary="Registrar un cobro de forma síncrona")
def procesar_cobro_sincrono(servicio_id: str, monto: float, usuario_id: str,
                            s: Session = Depends(sesion)):
    fila = EventoProcesadoFila(
        eventoId=nuevo_id(),
        tipo="COBRO_SINCRONO",
        contratacionId=servicio_id,
        prestadorId=None,
        monto=monto,
        resultado="COBRO_SINCRONO",
        origen="SINCRONO",
        fechaProcesado=datetime.now(),
    )
    s.add(fila)
    s.commit()
    return {"mensaje": "Cobro procesado exitosamente",
            "detalle": {**_cobro_legado(fila), "usuario_id": usuario_id}}


# --- Planes y comisiones ---------------------------------------------------
@app.get("/planes", tags=["planes"], summary="Tarifas vigentes de cada plan de prestador")
def listar_planes():
    return {
        "total": len(PLANES),
        "items": [{"plan": plan, **datos} for plan, datos in PLANES.items()],
        "umbralBloqueoBilletera": UMBRAL_BLOQUEO,
    }


class CobroComision(BaseModel):
    prestadorId: str
    contratacionId: str
    monto: float = Field(ge=0)
    medioPago: MedioPago = MedioPago.EFECTIVO


@app.post("/comisiones", tags=["billetera"], status_code=201,
          summary="Cargar a la billetera del prestador la comisión de una contratación")
def cobrar_comision(peticion: CobroComision, s: Session = Depends(sesion)):
    """Cobro síncrono de la comisión de un servicio pagado en efectivo.

    El camino normal es asíncrono (evento CONTRATACION_COMPLETADA → worker SQS).
    Este endpoint queda para cuando el sistema corre sin Pub/Sub; ambos usan la
    misma regla (`billetera.aplicar_comision`), idempotente por contratación.
    """
    billetera, movimiento, ya_cobrada = aplicar_comision(
        s, peticion.prestadorId, peticion.contratacionId, peticion.monto,
    )
    s.commit()
    return {
        "billetera": BilleteraPrestador.model_validate(billetera),
        "movimiento": MovimientoBilletera.model_validate(movimiento),
        "yaCobrada": ya_cobrada,
    }


class CambioPlan(BaseModel):
    prestadorId: str
    plan: PlanPrestador


@app.post("/suscripciones", tags=["suscripciones"], status_code=201,
          summary="Activar o cancelar la suscripción Pro de un prestador")
def cambiar_suscripcion(peticion: CambioPlan, s: Session = Depends(sesion)):
    activa = s.scalars(select(SuscripcionProFila).where(
        SuscripcionProFila.prestadorId == peticion.prestadorId,
        SuscripcionProFila.estado == "ACTIVA",
    )).first()

    if peticion.plan is PlanPrestador.FREE:
        if activa:
            activa.estado = "CANCELADA"
        s.commit()
        return {
            "prestadorId": peticion.prestadorId,
            "plan": PlanPrestador.FREE.value,
            "porcentajeComision": PLANES[PlanPrestador.FREE.value]["porcentajeComision"],
            "suscripcion": None,
        }

    if activa is None:
        hoy = date.today()
        activa = SuscripcionProFila(
            id=nuevo_id(),
            prestadorId=peticion.prestadorId,
            fechaInicio=hoy,
            fechaRenovacion=hoy + timedelta(days=30),
            estado="ACTIVA",
            valorMensual=PLANES[PlanPrestador.PRO.value]["valorMensual"],
        )
        s.add(activa)
    s.commit()
    return {
        "prestadorId": peticion.prestadorId,
        "plan": PlanPrestador.PRO.value,
        "porcentajeComision": PLANES[PlanPrestador.PRO.value]["porcentajeComision"],
        "suscripcion": SuscripcionPro.model_validate(activa),
    }


# --- Pagos -----------------------------------------------------------------
@app.get("/pagos", tags=["pagos"], summary="Listar pagos")
def listar_pagos(
    contratacionId: str | None = Query(None),
    estado: str | None = Query(None, description="APROBADO, PENDIENTE, RECHAZADO"),
    desde: date | None = Query(None),
    hasta: date | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(PagoFila).where(*condiciones(
        (PagoFila.contratacionId == contratacionId) if contratacionId else None,
        (func.upper(PagoFila.estado) == estado.upper()) if estado else None,
        (PagoFila.fechaPago >= desde) if desde else None,
        (PagoFila.fechaPago <= hasta) if hasta else None,
    )).order_by(PagoFila.fechaPago.desc())
    return paginar_consulta(s, consulta, Pago, page, size)


@app.get("/pagos/{pago_id}", tags=["pagos"], response_model=Pago, summary="Obtener un pago")
def obtener_pago(pago_id: str, s: Session = Depends(sesion)):
    fila = s.get(PagoFila, pago_id)
    if fila is None:
        raise HTTPException(404, f"Pago '{pago_id}' no encontrado")
    return Pago.model_validate(fila)


# --- Suscripciones ---------------------------------------------------------
@app.get("/suscripciones", tags=["suscripciones"], summary="Listar suscripciones Pro")
def listar_suscripciones(
    prestadorId: str | None = Query(None),
    estado: str | None = Query(None, description="ACTIVA, CANCELADA, VENCIDA"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(SuscripcionProFila).where(*condiciones(
        (SuscripcionProFila.prestadorId == prestadorId) if prestadorId else None,
        (func.upper(SuscripcionProFila.estado) == estado.upper()) if estado else None,
    )).order_by(SuscripcionProFila.fechaInicio.desc())
    return paginar_consulta(s, consulta, SuscripcionPro, page, size)


@app.get("/suscripciones/{suscripcion_id}", tags=["suscripciones"], response_model=SuscripcionPro,
         summary="Obtener una suscripción")
def obtener_suscripcion(suscripcion_id: str, s: Session = Depends(sesion)):
    fila = s.get(SuscripcionProFila, suscripcion_id)
    if fila is None:
        raise HTTPException(404, f"SuscripcionPro '{suscripcion_id}' no encontrada")
    return SuscripcionPro.model_validate(fila)


# --- Billeteras ------------------------------------------------------------
@app.get("/billeteras", tags=["billetera"], summary="Listar billeteras")
def listar_billeteras(
    prestadorId: str | None = Query(None),
    bloqueada: bool | None = Query(None),
    saldoMinimo: float | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(BilleteraPrestadorFila).where(*condiciones(
        (BilleteraPrestadorFila.prestadorId == prestadorId) if prestadorId else None,
        (BilleteraPrestadorFila.bloqueada == bloqueada) if bloqueada is not None else None,
        (BilleteraPrestadorFila.saldoPendiente >= saldoMinimo) if saldoMinimo is not None else None,
    ))
    return paginar_consulta(s, consulta, BilleteraPrestador, page, size)


@app.get("/billeteras/{billetera_id}", tags=["billetera"], response_model=BilleteraPrestador,
         summary="Obtener una billetera")
def obtener_billetera(billetera_id: str, s: Session = Depends(sesion)):
    fila = s.get(BilleteraPrestadorFila, billetera_id)
    if fila is None:
        raise HTTPException(404, f"BilleteraPrestador '{billetera_id}' no encontrada")
    return BilleteraPrestador.model_validate(fila)


@app.get("/billeteras/{billetera_id}/movimientos", tags=["billetera"],
         summary="Movimientos de una billetera")
def movimientos_de_billetera(
    billetera_id: str,
    tipo: str | None = Query(None, description="COMISION, ABONO_PAGO, RETIRO"),
    desde: date | None = Query(None),
    hasta: date | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    s: Session = Depends(sesion),
):
    if s.get(BilleteraPrestadorFila, billetera_id) is None:
        raise HTTPException(404, f"BilleteraPrestador '{billetera_id}' no encontrada")
    consulta = select(MovimientoBilleteraFila).where(*condiciones(
        MovimientoBilleteraFila.billeteraId == billetera_id,
        (func.upper(MovimientoBilleteraFila.tipo) == tipo.upper()) if tipo else None,
        (MovimientoBilleteraFila.fecha >= desde) if desde else None,
        (MovimientoBilleteraFila.fecha <= hasta) if hasta else None,
    )).order_by(MovimientoBilleteraFila.fecha.desc())
    return paginar_consulta(s, consulta, MovimientoBilletera, page, size)


@app.get("/movimientos", tags=["billetera"], summary="Listar movimientos de billetera")
def listar_movimientos(
    billeteraId: str | None = Query(None),
    contratacionId: str | None = Query(None),
    tipo: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    s: Session = Depends(sesion),
):
    consulta = select(MovimientoBilleteraFila).where(*condiciones(
        (MovimientoBilleteraFila.billeteraId == billeteraId) if billeteraId else None,
        (MovimientoBilleteraFila.contratacionId == contratacionId) if contratacionId else None,
        (func.upper(MovimientoBilleteraFila.tipo) == tipo.upper()) if tipo else None,
    )).order_by(MovimientoBilleteraFila.fecha.desc())
    return paginar_consulta(s, consulta, MovimientoBilletera, page, size)


# --- Lectura agregada ------------------------------------------------------
@app.get("/resumen/prestador/{prestador_id}", tags=["resumen"],
         summary="Estado financiero consolidado de un prestador")
def resumen_prestador(prestador_id: str, s: Session = Depends(sesion)):
    billetera_fila = s.scalars(
        select(BilleteraPrestadorFila)
        .where(BilleteraPrestadorFila.prestadorId == prestador_id)
    ).first()
    suscripcion_fila = s.scalars(
        select(SuscripcionProFila)
        .where(SuscripcionProFila.prestadorId == prestador_id,
               SuscripcionProFila.estado == "ACTIVA")
    ).first()

    totales_por_tipo: dict[str, float] = {}
    total_movimientos = 0
    if billetera_fila:
        filas = s.execute(
            select(MovimientoBilleteraFila.tipo, func.sum(MovimientoBilleteraFila.monto),
                   func.count())
            .where(MovimientoBilleteraFila.billeteraId == billetera_fila.id)
            .group_by(MovimientoBilleteraFila.tipo)
        ).all()
        totales_por_tipo = {tipo: float(suma) for tipo, suma, _ in filas}
        total_movimientos = sum(conteo for _, _, conteo in filas)

    return {
        "prestadorId": prestador_id,
        "billetera": BilleteraPrestador.model_validate(billetera_fila) if billetera_fila else None,
        "suscripcionActiva": (
            SuscripcionPro.model_validate(suscripcion_fila) if suscripcion_fila else None
        ),
        "totalMovimientos": total_movimientos,
        "totalesPorTipo": totales_por_tipo,
        "comisionesAcumuladas": abs(totales_por_tipo.get("COMISION", 0)),
    }
