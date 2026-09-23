"""Contexto delimitado: Contrataciones.

Dueño del ciclo de vida de la contratación (solicitud → check-in → check-out →
completada) y de la comisión calculada, en la base `jobbi_contrataciones`. Es el
productor del evento CONTRATACION_COMPLETADA que Monetización consume vía SNS/SQS.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from common.db import condiciones, inicializar, nuevo_id, paginar_consulta
from common.enums import EstadoContratacion, MedioPago, valores
from common.service import crear_servicio

from .models import AcuerdoTarifa, Contratacion
from .tablas import AcuerdoTarifaFila, ContratacionFila

app = crear_servicio(
    nombre="contrataciones",
    contexto="Contrataciones",
    descripcion=(
        "Ciclo de vida de la contratación entre demandante y prestador, con su "
        "valor acordado, medio de pago y comisión aplicada."
    ),
)

# Orden canónico del flujo; los estados terminales se tratan aparte.
_FLUJO = [
    EstadoContratacion.SOLICITADA,
    EstadoContratacion.ACEPTADA,
    EstadoContratacion.EN_CURSO,
    EstadoContratacion.CHECK_IN,
    EstadoContratacion.CHECK_OUT,
    EstadoContratacion.COMPLETADA,
]


Sesion: sessionmaker[Session] = inicializar("contrataciones")


def sesion() -> Session:
    with Sesion() as s:
        yield s


# --- Acuerdo de tarifa -----------------------------------------------------
# El chat por sí solo no crea nada. El servicio existe cuando las dos partes
# aceptan un valor, y ese momento es también el que fija la comisión.

class AltaAcuerdo(BaseModel):
    contactoId: str
    conversacionId: str | None = None
    demandanteId: str
    prestadorId: str
    oficioId: str
    valorPropuesto: float = Field(gt=0)
    medioPago: MedioPago = MedioPago.EFECTIVO
    propuestoPor: str = Field(pattern="^(DEMANDANTE|PRESTADOR)$")


class AceptacionAcuerdo(BaseModel):
    rol: str = Field(pattern="^(DEMANDANTE|PRESTADOR)$")
    # Quien orquesta (el gateway) consulta el plan vigente del prestador y lo
    # manda aquí: este contexto no cruza la frontera de Identidad ni la de
    # Monetización para averiguarlo.
    porcentajeComision: float = Field(default=0.18, ge=0, le=1)
    planPrestador: str | None = None


def _acuerdo_o_404(s: Session, acuerdo_id: str) -> AcuerdoTarifaFila:
    fila = s.get(AcuerdoTarifaFila, acuerdo_id)
    if fila is None:
        raise HTTPException(404, f"AcuerdoTarifa '{acuerdo_id}' no encontrado")
    return fila


@app.post("/acuerdos", tags=["acuerdos"], status_code=201,
          summary="Proponer una tarifa dentro de la conversación")
def alta_acuerdo(peticion: AltaAcuerdo, s: Session = Depends(sesion)):
    # Un contacto negocia una tarifa a la vez: proponer de nuevo reemplaza la
    # propuesta abierta en lugar de acumular botones en el chat.
    abierto = s.scalars(select(AcuerdoTarifaFila).where(
        AcuerdoTarifaFila.contactoId == peticion.contactoId,
        AcuerdoTarifaFila.estado == "PENDIENTE",
    )).first()
    if abierto:
        abierto.estado = "REEMPLAZADO"
        abierto.fechaCierre = datetime.now()

    acuerdo = AcuerdoTarifaFila(
        id=nuevo_id(),
        contactoId=peticion.contactoId,
        conversacionId=peticion.conversacionId,
        demandanteId=peticion.demandanteId,
        prestadorId=peticion.prestadorId,
        oficioId=peticion.oficioId,
        valorPropuesto=peticion.valorPropuesto,
        medioPago=peticion.medioPago.value,
        propuestoPor=peticion.propuestoPor,
        # Proponer es ya una aceptación de quien propone; falta la otra parte.
        aceptadoDemandante=peticion.propuestoPor == "DEMANDANTE",
        aceptadoPrestador=peticion.propuestoPor == "PRESTADOR",
        estado="PENDIENTE",
        fechaPropuesta=datetime.now(),
    )
    s.add(acuerdo)
    s.commit()
    return AcuerdoTarifa.model_validate(acuerdo)


@app.post("/acuerdos/{acuerdo_id}/aceptar", tags=["acuerdos"],
          summary="Aceptar la tarifa propuesta; si aceptan ambos, nace la contratación")
def aceptar_acuerdo(acuerdo_id: str, peticion: AceptacionAcuerdo, s: Session = Depends(sesion)):
    acuerdo = _acuerdo_o_404(s, acuerdo_id)
    if acuerdo.estado == "ACEPTADO":
        # Idempotente: dos toques al botón no crean dos servicios.
        contratacion = s.get(ContratacionFila, acuerdo.contratacionId)
        return {
            "acuerdo": AcuerdoTarifa.model_validate(acuerdo),
            "contratacion": Contratacion.model_validate(contratacion) if contratacion else None,
        }
    if acuerdo.estado != "PENDIENTE":
        raise HTTPException(409, f"El acuerdo ya está '{acuerdo.estado}'")

    if peticion.rol == "DEMANDANTE":
        acuerdo.aceptadoDemandante = True
    else:
        acuerdo.aceptadoPrestador = True

    contratacion = None
    if acuerdo.aceptadoDemandante and acuerdo.aceptadoPrestador:
        # Aquí se cierra el trato: el porcentaje que rige es el de este instante
        # y queda escrito en la contratación. Nada posterior lo recalcula.
        acuerdo.estado = "ACEPTADO"
        acuerdo.fechaCierre = datetime.now()
        acuerdo.porcentajeComisionCongelado = peticion.porcentajeComision
        acuerdo.planPrestadorAlAcordar = peticion.planPrestador

        contratacion = ContratacionFila(
            id=nuevo_id(),
            demandanteId=acuerdo.demandanteId,
            prestadorId=acuerdo.prestadorId,
            oficioId=acuerdo.oficioId,
            fechaSolicitud=date.today(),
            fechaEjecucion=None,
            estado=EstadoContratacion.ACEPTADA.value,
            valorAcordado=acuerdo.valorPropuesto,
            medioPago=acuerdo.medioPago,
            porcentajeComisionAplicado=peticion.porcentajeComision,
            montoComision=round(acuerdo.valorPropuesto * peticion.porcentajeComision, 2),
        )
        s.add(contratacion)
        s.flush()
        acuerdo.contratacionId = contratacion.id

    s.commit()
    return {
        "acuerdo": AcuerdoTarifa.model_validate(acuerdo),
        "contratacion": Contratacion.model_validate(contratacion) if contratacion else None,
    }


@app.post("/acuerdos/{acuerdo_id}/rechazar", tags=["acuerdos"],
          summary="Rechazar la tarifa propuesta")
def rechazar_acuerdo(acuerdo_id: str, s: Session = Depends(sesion)):
    acuerdo = _acuerdo_o_404(s, acuerdo_id)
    if acuerdo.estado != "PENDIENTE":
        raise HTTPException(409, f"El acuerdo ya está '{acuerdo.estado}'")
    acuerdo.estado = "RECHAZADO"
    acuerdo.fechaCierre = datetime.now()
    s.commit()
    return AcuerdoTarifa.model_validate(acuerdo)


@app.get("/acuerdos", tags=["acuerdos"], summary="Listar acuerdos de tarifa")
def listar_acuerdos(
    contactoId: str | None = Query(None),
    contactoIds: str | None = Query(
        None, description="Varios contactos separados por coma; así pide el gateway "
                          "de una vez el acuerdo vigente de toda la bandeja"),
    demandanteId: str | None = Query(None),
    prestadorId: str | None = Query(None),
    estado: str | None = Query(None, description="PENDIENTE, ACEPTADO, RECHAZADO, REEMPLAZADO"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    s: Session = Depends(sesion),
):
    lista = [c.strip() for c in contactoIds.split(",") if c.strip()] if contactoIds else None
    consulta = select(AcuerdoTarifaFila).where(*condiciones(
        (AcuerdoTarifaFila.contactoId == contactoId) if contactoId else None,
        AcuerdoTarifaFila.contactoId.in_(lista) if lista else None,
        (AcuerdoTarifaFila.demandanteId == demandanteId) if demandanteId else None,
        (AcuerdoTarifaFila.prestadorId == prestadorId) if prestadorId else None,
        (func.upper(AcuerdoTarifaFila.estado) == estado.upper()) if estado else None,
    )).order_by(AcuerdoTarifaFila.fechaPropuesta.desc())
    return paginar_consulta(s, consulta, AcuerdoTarifa, page, size)


@app.get("/acuerdos/{acuerdo_id}", tags=["acuerdos"], response_model=AcuerdoTarifa,
         summary="Obtener un acuerdo de tarifa")
def obtener_acuerdo(acuerdo_id: str, s: Session = Depends(sesion)):
    return AcuerdoTarifa.model_validate(_acuerdo_o_404(s, acuerdo_id))


# --- Ejecución del servicio ------------------------------------------------

def _contratacion_o_404(s: Session, contratacion_id: str) -> ContratacionFila:
    fila = s.get(ContratacionFila, contratacion_id)
    if fila is None:
        raise HTTPException(404, f"Contratacion '{contratacion_id}' no encontrada")
    return fila


@app.post("/contrataciones/{contratacion_id}/check-in", tags=["ejecución"],
          summary="El prestador marca su llegada y el servicio empieza")
def check_in(contratacion_id: str, s: Session = Depends(sesion)):
    fila = _contratacion_o_404(s, contratacion_id)
    if fila.checkIn is not None:
        return Contratacion.model_validate(fila)
    if fila.estado not in (EstadoContratacion.SOLICITADA.value, EstadoContratacion.ACEPTADA.value):
        raise HTTPException(409, f"No se puede hacer check-in con la contratación en '{fila.estado}'")

    fila.checkIn = datetime.now()
    fila.fechaEjecucion = date.today()
    fila.estado = EstadoContratacion.CHECK_IN.value
    s.commit()
    return Contratacion.model_validate(fila)


@app.post("/contrataciones/{contratacion_id}/check-out", tags=["ejecución"],
          summary="El prestador cierra el servicio, que queda completado")
def check_out(contratacion_id: str, s: Session = Depends(sesion)):
    fila = _contratacion_o_404(s, contratacion_id)
    if fila.checkOut is not None:
        return Contratacion.model_validate(fila)
    if fila.checkIn is None:
        raise HTTPException(409, "Hay que hacer check-in antes de cerrar el servicio")

    fila.checkOut = datetime.now()
    # El check-out es el cierre del trabajo: no queda ningún paso intermedio
    # entre marcar la salida y dar el servicio por completado.
    fila.estado = EstadoContratacion.COMPLETADA.value
    s.commit()
    return Contratacion.model_validate(fila)


@app.get("/contrataciones", tags=["contrataciones"], summary="Listar contrataciones")
def listar_contrataciones(
    demandanteId: str | None = Query(None),
    prestadorId: str | None = Query(None),
    oficioId: str | None = Query(None),
    estado: EstadoContratacion | None = Query(None),
    medioPago: MedioPago | None = Query(None),
    desde: date | None = Query(None, description="Fecha de solicitud mínima (YYYY-MM-DD)"),
    hasta: date | None = Query(None, description="Fecha de solicitud máxima (YYYY-MM-DD)"),
    valorMinimo: float | None = Query(None, ge=0),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    s: Session = Depends(sesion),
):
    consulta = select(ContratacionFila).where(*condiciones(
        (ContratacionFila.demandanteId == demandanteId) if demandanteId else None,
        (ContratacionFila.prestadorId == prestadorId) if prestadorId else None,
        (ContratacionFila.oficioId == oficioId) if oficioId else None,
        (ContratacionFila.estado == estado.value) if estado else None,
        (ContratacionFila.medioPago == medioPago.value) if medioPago else None,
        (ContratacionFila.fechaSolicitud >= desde) if desde else None,
        (ContratacionFila.fechaSolicitud <= hasta) if hasta else None,
        (ContratacionFila.valorAcordado >= valorMinimo) if valorMinimo is not None else None,
    )).order_by(ContratacionFila.fechaSolicitud.desc())
    return paginar_consulta(s, consulta, Contratacion, page, size)


@app.get("/contrataciones/{contratacion_id}", tags=["contrataciones"], response_model=Contratacion,
         summary="Obtener una contratación")
def obtener_contratacion(contratacion_id: str, s: Session = Depends(sesion)):
    fila = s.get(ContratacionFila, contratacion_id)
    if fila is None:
        raise HTTPException(404, f"Contratacion '{contratacion_id}' no encontrada")
    return Contratacion.model_validate(fila)


@app.get("/contrataciones/{contratacion_id}/timeline", tags=["contrataciones"],
         summary="Línea de tiempo del estado de una contratación")
def timeline(contratacion_id: str, s: Session = Depends(sesion)):
    fila = s.get(ContratacionFila, contratacion_id)
    if fila is None:
        raise HTTPException(404, f"Contratacion '{contratacion_id}' no encontrada")
    c = Contratacion.model_validate(fila)

    terminal_fuera_de_flujo = c.estado in (
        EstadoContratacion.CANCELADA,
        EstadoContratacion.EN_DISPUTA,
    )

    if terminal_fuera_de_flujo:
        # CANCELADA y EN_DISPUTA salen del flujo lineal, así que hasta dónde se
        # avanzó no se deduce del estado sino de las marcas de tiempo reales.
        # En particular, una disputa nunca llega a COMPLETADA.
        alcanzado_por_paso = {
            EstadoContratacion.SOLICITADA: True,
            EstadoContratacion.ACEPTADA: c.fechaEjecucion is not None,
            EstadoContratacion.EN_CURSO: c.checkIn is not None,
            EstadoContratacion.CHECK_IN: c.checkIn is not None,
            EstadoContratacion.CHECK_OUT: c.checkOut is not None,
            EstadoContratacion.COMPLETADA: False,
        }
        pasos = [
            {"estado": estado.value, "alcanzado": alcanzado_por_paso[estado]}
            for estado in _FLUJO
        ]
        pasos.append({"estado": c.estado.value, "alcanzado": True})
    else:
        alcanzados = _FLUJO.index(c.estado) + 1
        pasos = [
            {"estado": estado.value, "alcanzado": indice < alcanzados}
            for indice, estado in enumerate(_FLUJO)
        ]

    return {
        "contratacionId": c.id,
        "estadoActual": c.estado,
        "fechaSolicitud": c.fechaSolicitud,
        "fechaEjecucion": c.fechaEjecucion,
        "checkIn": c.checkIn,
        "checkOut": c.checkOut,
        "duracionMinutos": (
            int((c.checkOut - c.checkIn).total_seconds() // 60)
            if c.checkIn and c.checkOut
            else None
        ),
        "pasos": pasos,
    }


@app.get("/resumen", tags=["resumen"], summary="Totales por estado y comisión acumulada")
def resumen(
    prestadorId: str | None = Query(None),
    demandanteId: str | None = Query(None),
    s: Session = Depends(sesion),
):
    filtros = condiciones(
        (ContratacionFila.prestadorId == prestadorId) if prestadorId else None,
        (ContratacionFila.demandanteId == demandanteId) if demandanteId else None,
    )

    # Conteo por estado con GROUP BY, no recorriendo filas en Python.
    por_estado = dict(s.execute(
        select(ContratacionFila.estado, func.count())
        .where(*filtros)
        .group_by(ContratacionFila.estado)
    ).all())

    completadas = condiciones(*filtros, ContratacionFila.estado == EstadoContratacion.COMPLETADA.value)
    total_completadas, valor, comision = s.execute(
        select(func.count(), func.coalesce(func.sum(ContratacionFila.valorAcordado), 0),
               func.coalesce(func.sum(ContratacionFila.montoComision), 0))
        .where(*completadas)
    ).one()

    return {
        "total": sum(por_estado.values()),
        "porEstado": por_estado,
        "totalCompletadas": total_completadas,
        "valorTotalCompletado": float(valor),
        "comisionTotalCompletada": float(comision),
        "ticketPromedio": round(float(valor) / total_completadas, 2) if total_completadas else 0,
    }


@app.get("/enums", tags=["catálogos"], summary="Enumeraciones que expone este contexto")
def enumeraciones():
    return {
        "estadoContratacion": valores(EstadoContratacion),
        "medioPago": valores(MedioPago),
        "flujo": [e.value for e in _FLUJO],
    }
