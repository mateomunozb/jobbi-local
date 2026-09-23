"""Contexto delimitado: Monetización.

Dueño de pagos, suscripciones Pro, billeteras y movimientos, en la base
`jobbi_monetizacion`. Conserva el worker SQS y los endpoints /cobros y /cobrar
del simulador original, de modo que la prueba de carga k6 y el flujo Pub/Sub del
README siguen funcionando igual.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from common.db import condiciones, inicializar, nuevo_id, paginar_consulta
from common.enums import MedioPago, PlanPrestador
from common.service import crear_servicio

from .models import BilleteraPrestador, MovimientoBilletera, Pago, SuscripcionPro
from .sqs_worker import COBROS_REGISTRADOS, iniciar_worker
from .tablas import BilleteraPrestadorFila, MovimientoBilleteraFila, PagoFila, SuscripcionProFila

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

# Por encima de este saldo la billetera se bloquea: la comisión en efectivo la
# cobra el prestador de su cliente y queda debiéndosela a la plataforma, así que
# acumular deuda sin liquidar no puede ser gratis.
UMBRAL_BLOQUEO = 150000.0


def sesion() -> Session:
    with Sesion() as s:
        yield s


@app.on_event("startup")
def _arrancar_worker() -> None:
    global _worker_activo
    _worker_activo = iniciar_worker()


# --- Integración Pub/Sub (compatibilidad con el simulador original) --------
@app.get("/cobros", tags=["pub/sub"], summary="Cobros acumulados por el worker SQS")
def listar_cobros():
    return {"total": len(COBROS_REGISTRADOS), "cobros": COBROS_REGISTRADOS}


@app.get("/cobros/estado-worker", tags=["pub/sub"], summary="Estado del consumidor SQS")
def estado_worker():
    return {"workerActivo": _worker_activo, "cobrosProcesados": len(COBROS_REGISTRADOS)}


@app.post("/cobrar", tags=["pub/sub"], summary="Registrar un cobro de forma síncrona")
def procesar_cobro_sincrono(servicio_id: str, monto: float, usuario_id: str):
    registro = {
        "id": f"COBRO-{len(COBROS_REGISTRADOS) + 1}",
        "servicio_id": servicio_id,
        "monto": monto,
        "usuario_id": usuario_id,
        "tipo": "SINCRONO",
    }
    COBROS_REGISTRADOS.append(registro)
    return {"mensaje": "Cobro procesado exitosamente", "detalle": registro}


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


def _billetera_de(s: Session, prestador_id: str) -> BilleteraPrestadorFila:
    """Devuelve la billetera del prestador, creándola en su primer movimiento.

    No se abre en el registro: un prestador que nunca ha trabajado no tiene
    nada que liquidar, y la base no guarda filas que no representen un hecho.
    """
    billetera = s.scalars(select(BilleteraPrestadorFila).where(
        BilleteraPrestadorFila.prestadorId == prestador_id
    )).first()
    if billetera is None:
        billetera = BilleteraPrestadorFila(
            id=nuevo_id(), prestadorId=prestador_id, saldoPendiente=0.0, bloqueada=False,
        )
        s.add(billetera)
        s.flush()
    return billetera


@app.post("/comisiones", tags=["billetera"], status_code=201,
          summary="Cargar a la billetera del prestador la comisión de una contratación")
def cobrar_comision(peticion: CobroComision, s: Session = Depends(sesion)):
    """El cobro del servicio pagado en efectivo.

    El dinero del servicio nunca pasa por la plataforma: lo recibe el prestador
    de su cliente. Lo que queda es la comisión, que se le carga a su billetera
    como saldo pendiente de liquidar. El monto llega ya calculado, con el
    porcentaje que se congeló al cerrar el acuerdo.
    """
    billetera = _billetera_de(s, peticion.prestadorId)

    # Idempotente por contratación: reintentar el check-out no cobra dos veces.
    existente = s.scalars(select(MovimientoBilleteraFila).where(
        MovimientoBilleteraFila.billeteraId == billetera.id,
        MovimientoBilleteraFila.contratacionId == peticion.contratacionId,
        MovimientoBilleteraFila.tipo == "COMISION",
    )).first()
    if existente:
        return {
            "billetera": BilleteraPrestador.model_validate(billetera),
            "movimiento": MovimientoBilletera.model_validate(existente),
            "yaCobrada": True,
        }

    movimiento = MovimientoBilleteraFila(
        id=nuevo_id(),
        billeteraId=billetera.id,
        contratacionId=peticion.contratacionId,
        tipo="COMISION",
        # Negativo: es lo que el prestador le debe a la plataforma.
        monto=-abs(peticion.monto),
        fecha=date.today(),
    )
    s.add(movimiento)
    billetera.saldoPendiente = round(billetera.saldoPendiente + abs(peticion.monto), 2)
    billetera.bloqueada = billetera.saldoPendiente > UMBRAL_BLOQUEO
    s.commit()
    return {
        "billetera": BilleteraPrestador.model_validate(billetera),
        "movimiento": MovimientoBilletera.model_validate(movimiento),
        "yaCobrada": False,
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
