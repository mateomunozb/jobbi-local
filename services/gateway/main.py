"""API Gateway de JOBBI.

Punto de entrada único del backend. Cumple tres funciones:

1. **Enrutamiento** (`/api/{servicio}/...`): reenvía la petición al microservicio
   dueño del contexto, sin reinterpretar su contrato.
2. **Composición BFF** (`/api/bff/...`): agrega en una sola respuesta lo que una
   pantalla del frontend necesita de varios contextos, para que el navegador no
   tenga que hacer seis llamadas y conocer la topología interna.
3. **Observabilidad** (`/health/servicios`): estado agregado del backend.

El gateway nunca cruza fronteras por su cuenta: cada dato lo pide al servicio
que lo posee, y si un contexto no responde lo marca como degradado en lugar de
tumbar la respuesta completa.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse

from common.service import crear_servicio

from .registro import SERVICIOS

app = crear_servicio(
    nombre="gateway",
    contexto="API Gateway",
    descripcion=(
        "Punto de entrada único: enruta hacia los microservicios de dominio y "
        "compone vistas agregadas para el frontend."
    ),
)

TIMEOUT = httpx.Timeout(10.0, connect=3.0)
_cliente: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _abrir_cliente() -> None:
    global _cliente
    _cliente = httpx.AsyncClient(timeout=TIMEOUT)


@app.on_event("shutdown")
async def _cerrar_cliente() -> None:
    if _cliente:
        await _cliente.aclose()


async def _pedir(servicio: str, ruta: str, params: dict | None = None) -> Any:
    """Llama a un servicio de dominio. Devuelve None si el contexto no responde."""
    base = SERVICIOS[servicio]["url"]
    try:
        respuesta = await _cliente.get(f"{base}{ruta}", params=params)
    except httpx.HTTPError:
        return None
    if respuesta.status_code >= 400:
        return None
    return respuesta.json()


async def _enviar(servicio: str, ruta: str, cuerpo: dict) -> JSONResponse:
    """Reenvía un POST al servicio dueño, propagando su código de estado."""
    base = SERVICIOS[servicio]["url"]
    try:
        respuesta = await _cliente.post(f"{base}{ruta}", json=cuerpo)
    except httpx.HTTPError as e:
        raise HTTPException(503, f"El servicio '{servicio}' no está disponible: {e}") from e
    return JSONResponse(status_code=respuesta.status_code, content=respuesta.json())


async def _enviar_json(servicio: str, ruta: str, cuerpo: dict) -> Any:
    """Como `_enviar`, pero devuelve el cuerpo ya decodificado, o None si falla.

    Lo usan las composiciones BFF, que encadenan varias altas y necesitan el
    resultado de una para construir la siguiente.
    """
    base = SERVICIOS[servicio]["url"]
    try:
        respuesta = await _cliente.post(f"{base}{ruta}", json=cuerpo)
    except httpx.HTTPError:
        return None
    if respuesta.status_code >= 400:
        return None
    return respuesta.json()


# --- Autenticación ---------------------------------------------------------
@app.post("/api/auth/registro", tags=["auth"], status_code=201,
          summary="Registrar un usuario y activar su perfil")
async def registro(cuerpo: dict):
    return await _enviar("identidad", "/auth/registro", cuerpo)


@app.post("/api/auth/login", tags=["auth"], summary="Iniciar sesión solo con el correo")
async def login(cuerpo: dict):
    return await _enviar("identidad", "/auth/login", cuerpo)


# --- Observabilidad --------------------------------------------------------
@app.get("/health/servicios", tags=["operación"],
         summary="Estado de todos los microservicios de dominio")
async def health_servicios():
    async def sondear(nombre: str) -> dict:
        datos = await _pedir(nombre, "/health")
        return {
            "servicio": nombre,
            "contexto": SERVICIOS[nombre]["contexto"],
            "url": SERVICIOS[nombre]["url"],
            "status": datos.get("status") if datos else "DOWN",
        }

    resultados = await asyncio.gather(*(sondear(n) for n in SERVICIOS))
    caidos = [r for r in resultados if r["status"] != "UP"]
    return {
        "status": "UP" if not caidos else "DEGRADED",
        "totalServicios": len(resultados),
        "disponibles": len(resultados) - len(caidos),
        "servicios": resultados,
    }


@app.get("/api/servicios", tags=["operación"],
         summary="Catálogo de contextos enrutados por el gateway")
def catalogo_servicios():
    return {
        "total": len(SERVICIOS),
        "servicios": [
            {
                "nombre": nombre,
                "contexto": info["contexto"],
                "prefijoGateway": f"/api/{nombre}",
                # El Swagger de cada servicio se abre contra el servicio directo,
                # no a través del proxy (sus assets usan rutas relativas).
                "docsDelServicio": f"{info['url']}/docs",
            }
            for nombre, info in SERVICIOS.items()
        ],
    }


# --- Composición BFF -------------------------------------------------------
# Se declaran ANTES del proxy genérico: FastAPI resuelve las rutas en orden y
# "bff" no es un servicio registrado.
@app.get("/api/bff/prestadores/{prestador_id}", tags=["bff"],
         summary="Ficha completa de un prestador (5 contextos en una llamada)")
async def bff_prestador(prestador_id: str):
    perfil, oficios, verificacion, resenas, finanzas, referidos = await asyncio.gather(
        _pedir("identidad", f"/prestadores/{prestador_id}"),
        _pedir("mercado", f"/prestadores/{prestador_id}/oficios"),
        _pedir("confianza", f"/prestadores/{prestador_id}/estado-verificacion"),
        _pedir("confianza", "/resenas", {"receptorId": prestador_id, "size": 5}),
        _pedir("monetizacion", f"/resumen/prestador/{prestador_id}"),
        _pedir("adquisicion", "/referidos", {"prestadorId": prestador_id}),
    )
    if perfil is None:
        raise HTTPException(404, f"Prestador '{prestador_id}' no encontrado en Identidad")

    contrataciones = await _pedir(
        "contrataciones", "/resumen", {"prestadorId": prestador_id}
    )
    return {
        "perfil": perfil,
        "oficios": oficios,
        "verificacion": verificacion,
        "ultimasResenas": resenas,
        "contrataciones": contrataciones,
        "finanzas": finanzas,
        "canalDeOrigen": referidos,
    }


@app.get("/api/bff/contrataciones/{contratacion_id}", tags=["bff"],
         summary="Detalle 360° de una contratación")
async def bff_contratacion(contratacion_id: str):
    contratacion = await _pedir("contrataciones", f"/contrataciones/{contratacion_id}")
    if contratacion is None:
        raise HTTPException(404, f"Contratación '{contratacion_id}' no encontrada")

    timeline, pagos, resenas, incidentes, proteccion = await asyncio.gather(
        _pedir("contrataciones", f"/contrataciones/{contratacion_id}/timeline"),
        _pedir("monetizacion", "/pagos", {"contratacionId": contratacion_id}),
        _pedir("confianza", "/resenas", {"contratacionId": contratacion_id}),
        _pedir("soporte", "/incidentes", {"contratacionId": contratacion_id}),
        _pedir("proteccion", "/planes-proteccion", {"contratacionId": contratacion_id}),
    )
    demandante, prestador, oficio = await asyncio.gather(
        _pedir("identidad", f"/demandantes/{contratacion['demandanteId']}"),
        _pedir("identidad", f"/prestadores/{contratacion['prestadorId']}"),
        _pedir("mercado", f"/oficios/{contratacion['oficioId']}"),
    )
    return {
        "contratacion": contratacion,
        "timeline": timeline,
        "demandante": demandante,
        "prestador": prestador,
        "oficio": oficio,
        "pagos": pagos,
        "resenas": resenas,
        "incidentes": incidentes,
        "proteccion": proteccion,
    }


@app.get("/api/bff/demandantes/{demandante_id}/inicio", tags=["bff"],
         summary="Pantalla de inicio del demandante")
async def bff_inicio_demandante(demandante_id: str):
    perfil = await _pedir("identidad", f"/demandantes/{demandante_id}")
    if perfil is None:
        raise HTTPException(404, f"Demandante '{demandante_id}' no encontrado")

    contactos, contrataciones, busquedas, notificaciones = await asyncio.gather(
        _pedir("mercado", "/contactos", {"demandanteId": demandante_id}),
        _pedir("contrataciones", "/contrataciones", {"demandanteId": demandante_id, "size": 5}),
        _pedir("mercado", "/busquedas", {"demandanteId": demandante_id, "size": 5}),
        _pedir("comunicacion", "/notificaciones/resumen", {"usuarioId": perfil["usuarioId"]}),
    )
    return {
        "perfil": perfil,
        "contactos": contactos,
        "ultimasContrataciones": contrataciones,
        "ultimasBusquedas": busquedas,
        "notificaciones": notificaciones,
    }


@app.get("/api/bff/catalogo", tags=["bff"],
         summary="Catálogo de búsqueda con prestadores destacados")
async def bff_catalogo(
    categoriaId: str | None = Query(None),
    calificacionMinima: float | None = Query(None, ge=0, le=5),
    municipio: str | None = Query(None),
):
    filtros_prestador = {"orden": "calificacion", "size": 20}
    if calificacionMinima is not None:
        filtros_prestador["calificacionMinima"] = calificacionMinima
    if municipio:
        filtros_prestador["municipio"] = municipio

    categorias, oficios, prestadores, ofertas = await asyncio.gather(
        _pedir("mercado", "/categorias"),
        _pedir("mercado", "/oficios", {"categoriaId": categoriaId} if categoriaId else None),
        _pedir("identidad", "/prestadores", filtros_prestador),
        _pedir("mercado", "/prestador-oficios", {"size": 200}),
    )

    # El listado necesita mostrar qué oficio ofrece cada prestador, pero el
    # nombre del oficio vive en Mercado y el perfil en Identidad. En vez de que
    # el cliente haga una petición por tarjeta, aquí se traen las dos colecciones
    # completas una sola vez y se cruzan por oficioId.
    catalogo_oficios = {o["id"]: o for o in ((oficios or {}).get("items") or [])}
    ofertas_por_prestador: dict[str, list[dict]] = {}
    for oferta in (ofertas or {}).get("items") or []:
        oficio = catalogo_oficios.get(oferta["oficioId"])
        if oficio is None:
            # El oficio quedó fuera del filtro por categoría: no aplica.
            continue
        ofertas_por_prestador.setdefault(oferta["prestadorId"], []).append({
            "oficioId": oficio["id"],
            "nombre": oficio["nombre"],
            "categoriaId": oficio["categoriaId"],
            "tarifaReferencial": oferta["tarifaReferencial"],
            "anosExperiencia": oferta["anosExperiencia"],
        })

    items = []
    for prestador in (prestadores or {}).get("items") or []:
        propias = ofertas_por_prestador.get(prestador["id"], [])
        # Si se filtró por categoría, solo interesan quienes ofrecen algo en ella.
        if categoriaId and not propias:
            continue
        items.append({**prestador, "ofertas": propias})

    return {
        "categorias": categorias,
        "oficios": oficios,
        "prestadores": {**(prestadores or {}), "items": items, "total": len(items)},
    }


@app.get("/api/bff/admin/metricas", tags=["bff"],
         summary="Tablero de métricas para el rol Admin")
async def bff_metricas_admin():
    contrataciones, catalogo, incidentes, canal, moderacion = await asyncio.gather(
        _pedir("contrataciones", "/resumen"),
        _pedir("mercado", "/resumen/catalogo"),
        _pedir("soporte", "/incidentes/resumen"),
        _pedir("adquisicion", "/resumen/canal"),
        _pedir("confianza", "/moderacion/cola"),
    )
    prestadores = await _pedir("identidad", "/prestadores", {"size": 100})
    return {
        "contrataciones": contrataciones,
        "catalogo": catalogo,
        "incidentes": incidentes,
        "canalAdquisicion": canal,
        "resenasEnModeracion": (moderacion or {}).get("total", 0),
        "totalPrestadores": (prestadores or {}).get("total", 0),
    }


@app.get("/api/bff/mensajes", tags=["bff"],
         summary="Bandeja de conversaciones de una persona, con la otra parte resuelta")
async def bff_mensajes(
    demandanteId: str | None = Query(None),
    prestadorId: str | None = Query(None),
):
    """La bandeja cruza tres contextos y ninguno puede resolverla solo.

    Los contactos son de Mercado, las conversaciones de Comunicación, y el
    nombre de la otra parte de Identidad. Comunicación no sabe quién participa
    en un hilo —solo de qué contacto cuelga—, así que preguntar por contacto es
    lo que hace visible una conversación recién abierta, aún sin mensajes.
    """
    if not demandanteId and not prestadorId:
        raise HTTPException(422, "Indica 'demandanteId' o 'prestadorId'")

    filtro = {"demandanteId": demandanteId} if demandanteId else {"prestadorId": prestadorId}
    contactos = await _pedir("mercado", "/contactos", {**filtro, "size": 100})
    items_contacto = (contactos or {}).get("items") or []
    if not items_contacto:
        return {"total": 0, "items": []}

    por_id = {c["id"]: c for c in items_contacto}
    conversaciones = await _pedir("comunicacion", "/conversaciones", {
        "contactoIds": ",".join(por_id), "size": 100,
    })

    # El nombre de la contraparte se pide una vez por persona, no por hilo.
    soy_demandante = bool(demandanteId)
    otros = {c["prestadorId"] if soy_demandante else c["demandanteId"] for c in items_contacto}
    ruta = "/prestadores/{}" if soy_demandante else "/demandantes/{}"
    perfiles = await asyncio.gather(*(_pedir("identidad", ruta.format(o)) for o in otros))
    nombres = {
        perfil["id"]: perfil.get("nombreCompleto", "")
        for perfil in perfiles if perfil
    }

    # El chat es donde se negocia la tarifa, así que la bandeja trae ya el
    # acuerdo vigente de cada hilo: sin él la pantalla no sabría si mostrar el
    # botón de proponer, el de aceptar, o el servicio ya confirmado.
    acuerdos = await _pedir("contrataciones", "/acuerdos", {
        "contactoIds": ",".join(por_id), "size": 200,
    })
    vigente_por_contacto: dict[str, dict] = {}
    for acuerdo in (acuerdos or {}).get("items") or []:
        # Vienen del más reciente al más antiguo: el primero útil de cada
        # contacto es el que manda.
        if acuerdo["estado"] not in ("PENDIENTE", "ACEPTADO"):
            continue
        vigente_por_contacto.setdefault(acuerdo["contactoId"], acuerdo)

    contrataciones = await asyncio.gather(*(
        _pedir("contrataciones", f"/contrataciones/{a['contratacionId']}")
        for a in vigente_por_contacto.values() if a.get("contratacionId")
    ))
    por_contratacion = {c["id"]: c for c in contrataciones if c}

    items = []
    for conversacion in (conversaciones or {}).get("items") or []:
        contacto = por_id.get(conversacion["contactoId"])
        if contacto is None:
            continue
        otro = contacto["prestadorId"] if soy_demandante else contacto["demandanteId"]
        acuerdo = vigente_por_contacto.get(contacto["id"])
        items.append({
            **conversacion,
            "contacto": contacto,
            "otraParteId": otro,
            "otraParteNombre": nombres.get(otro, "Usuario"),
            "acuerdo": acuerdo,
            "contratacion": por_contratacion.get((acuerdo or {}).get("contratacionId")),
        })
    return {"total": len(items), "items": items}


@app.post("/api/bff/contactar", tags=["bff"], status_code=201,
          summary="Abrir contacto y conversación entre un demandante y un prestador")
async def bff_contactar(cuerpo: dict):
    """Un solo paso para el frontend, dos contextos por debajo.

    Pulsar "Contactar" tiene que dejar el chat listo, pero el Contacto pertenece
    a Mercado y la Conversación a Comunicación. El gateway encadena ambas altas
    y devuelve lo necesario para abrir la pantalla; las dos son idempotentes,
    así que volver a contactar a la misma persona reabre el mismo hilo.
    """
    demandante_id = cuerpo.get("demandanteId")
    prestador_id = cuerpo.get("prestadorId")
    if not demandante_id or not prestador_id:
        raise HTTPException(422, "Se requieren 'demandanteId' y 'prestadorId'")

    contacto = await _enviar_json("mercado", "/contactos", {
        "demandanteId": demandante_id,
        "prestadorId": prestador_id,
        "busquedaOrigenId": cuerpo.get("busquedaOrigenId"),
    })
    if contacto is None:
        raise HTTPException(503, "El contexto de Mercado no pudo abrir el contacto")

    conversacion = await _enviar_json("comunicacion", "/conversaciones", {
        "contactoId": contacto["id"],
    })
    if conversacion is None:
        # El contacto quedó creado; solo falla la mensajería. Se informa en vez
        # de fingir que el chat está listo.
        raise HTTPException(503, "El contexto de Comunicación no pudo abrir la conversación")

    return {"contacto": contacto, "conversacion": conversacion}


# --- Acuerdo de tarifa y ejecución del servicio ----------------------------
# Todo este bloque existe porque el flujo real cruza contextos: la tarifa se
# acuerda en Contrataciones, el porcentaje que la grava depende del plan que
# guarda Identidad y de la tarifa que fija Monetización, y cada paso se avisa
# por Comunicación. Ningún contexto puede (ni debe) hacerlo solo.

async def _avisar(usuario_id: str | None, tipo: str, contenido: str) -> None:
    """Deja una notificación. Que falle no puede tumbar la operación real."""
    if not usuario_id:
        return
    await _enviar_json("comunicacion", "/notificaciones", {
        "usuarioId": usuario_id, "tipo": tipo, "canal": "PUSH", "contenido": contenido,
    })


async def _usuarios_de(demandante_id: str, prestador_id: str) -> tuple[str | None, str | None]:
    """usuarioId de cada parte, que es a quien se le notifica."""
    demandante, prestador = await asyncio.gather(
        _pedir("identidad", f"/demandantes/{demandante_id}"),
        _pedir("identidad", f"/prestadores/{prestador_id}"),
    )
    return (
        (demandante or {}).get("usuarioId"),
        (prestador or {}).get("usuarioId"),
    )


async def _comision_vigente(prestador_id: str) -> tuple[float, str]:
    """Porcentaje que le corresponde hoy al prestador, y el plan que lo explica.

    Es lo que se congela al cerrar un acuerdo. El plan lo sabe Identidad y la
    tarifa de cada plan la fija Monetización: el gateway solo los cruza.
    """
    perfil, planes = await asyncio.gather(
        _pedir("identidad", f"/prestadores/{prestador_id}"),
        _pedir("monetizacion", "/planes"),
    )
    plan = (perfil or {}).get("planActual", "FREE")
    tarifas = {p["plan"]: p["porcentajeComision"] for p in (planes or {}).get("items", [])}
    # Si Monetización no responde, se aplica la tarifa del plan gratuito: nunca
    # se cobra de menos por un contexto caído.
    return tarifas.get(plan, tarifas.get("FREE", 0.18)), plan


@app.get("/api/bff/comision-vigente/{prestador_id}", tags=["bff"],
         summary="Comisión que se congelaría hoy para este prestador")
async def bff_comision_vigente(prestador_id: str):
    porcentaje, plan = await _comision_vigente(prestador_id)
    return {"prestadorId": prestador_id, "plan": plan, "porcentajeComision": porcentaje}


@app.post("/api/bff/acuerdos", tags=["bff"], status_code=201,
          summary="Proponer la tarifa del servicio desde el chat")
async def bff_proponer_acuerdo(cuerpo: dict):
    demandante_id = cuerpo.get("demandanteId")
    prestador_id = cuerpo.get("prestadorId")
    contacto_id = cuerpo.get("contactoId")
    if not (demandante_id and prestador_id and contacto_id):
        raise HTTPException(422, "Se requieren 'contactoId', 'demandanteId' y 'prestadorId'")

    oficio_id = cuerpo.get("oficioId")
    valor = cuerpo.get("valorPropuesto")
    if not oficio_id or valor is None:
        # Lo que se negocia es un oficio concreto con su tarifa; si el chat no
        # los manda, se toman de la oferta que el prestador ya publicó.
        oficios = await _pedir("mercado", f"/prestadores/{prestador_id}/oficios")
        items = (oficios or {}).get("items") or []
        if not items:
            raise HTTPException(409, "El prestador no tiene ningún oficio publicado todavía")
        oficio_id = oficio_id or items[0]["oficio"]["id"]
        if valor is None:
            valor = items[0]["tarifaReferencial"]

    acuerdo = await _enviar_json("contrataciones", "/acuerdos", {
        "contactoId": contacto_id,
        "conversacionId": cuerpo.get("conversacionId"),
        "demandanteId": demandante_id,
        "prestadorId": prestador_id,
        "oficioId": oficio_id,
        "valorPropuesto": valor,
        "medioPago": cuerpo.get("medioPago", "EFECTIVO"),
        "propuestoPor": cuerpo.get("propuestoPor", "DEMANDANTE"),
    })
    if acuerdo is None:
        raise HTTPException(503, "El contexto de Contrataciones no pudo registrar la propuesta")

    porcentaje, plan = await _comision_vigente(prestador_id)
    usuario_dem, usuario_pres = await _usuarios_de(demandante_id, prestador_id)
    destino = usuario_pres if acuerdo["propuestoPor"] == "DEMANDANTE" else usuario_dem
    await _avisar(destino, "TARIFA_PROPUESTA",
                  f"Te proponen un servicio por ${round(acuerdo['valorPropuesto']):,} COP.")
    # El porcentaje viaja como información: todavía no está congelado, y no lo
    # estará hasta que la otra parte acepte.
    return {"acuerdo": acuerdo, "comisionVigente": {"plan": plan, "porcentaje": porcentaje}}


@app.post("/api/bff/acuerdos/{acuerdo_id}/aceptar", tags=["bff"],
          summary="Aceptar la tarifa; al aceptar ambos nace el servicio con su comisión congelada")
async def bff_aceptar_acuerdo(acuerdo_id: str, cuerpo: dict):
    rol = cuerpo.get("rol")
    if rol not in ("DEMANDANTE", "PRESTADOR"):
        raise HTTPException(422, "Indica 'rol': DEMANDANTE o PRESTADOR")

    acuerdo_previo = await _pedir("contrataciones", f"/acuerdos/{acuerdo_id}")
    if acuerdo_previo is None:
        raise HTTPException(404, f"Acuerdo '{acuerdo_id}' no encontrado")

    porcentaje, plan = await _comision_vigente(acuerdo_previo["prestadorId"])
    resultado = await _enviar_json("contrataciones", f"/acuerdos/{acuerdo_id}/aceptar", {
        "rol": rol, "porcentajeComision": porcentaje, "planPrestador": plan,
    })
    if resultado is None:
        raise HTTPException(503, "El contexto de Contrataciones no pudo cerrar el acuerdo")

    contratacion = resultado.get("contratacion")
    if contratacion:
        usuario_dem, usuario_pres = await _usuarios_de(
            contratacion["demandanteId"], contratacion["prestadorId"])
        texto = (f"Servicio confirmado por ${round(contratacion['valorAcordado']):,} COP "
                 f"(comisión {round(contratacion['porcentajeComisionAplicado'] * 100)}% "
                 f"del plan {plan}).")
        await asyncio.gather(
            _avisar(usuario_dem, "SERVICIO_CONFIRMADO", texto),
            _avisar(usuario_pres, "SERVICIO_CONFIRMADO", texto),
        )
    return resultado


@app.post("/api/bff/contrataciones/{contratacion_id}/check-in", tags=["bff"],
          summary="Check-in del prestador")
async def bff_check_in(contratacion_id: str, cuerpo: dict | None = None):
    contratacion = await _enviar_json(
        "contrataciones", f"/contrataciones/{contratacion_id}/check-in", {})
    if contratacion is None:
        raise HTTPException(409, "No se pudo registrar el check-in de esta contratación")

    usuario_dem, _ = await _usuarios_de(
        contratacion["demandanteId"], contratacion["prestadorId"])
    await _avisar(usuario_dem, "SERVICIO_INICIADO",
                  "Tu prestador hizo check-in: el servicio está en curso.")
    return {"contratacion": contratacion}


@app.post("/api/bff/contrataciones/{contratacion_id}/check-out", tags=["bff"],
          summary="Check-out del prestador: cierra el servicio y cobra la comisión")
async def bff_check_out(contratacion_id: str, cuerpo: dict | None = None):
    """El cierre del servicio es también el momento del cobro.

    Con pago en efectivo el dinero no pasa por la plataforma, así que no hay
    nada que retener: lo que queda es cargarle al prestador la comisión pactada
    en su billetera. Se usa `montoComision`, que Contrataciones calculó con el
    porcentaje congelado al cerrar el acuerdo; el plan que tenga hoy no entra
    en esta cuenta.
    """
    contratacion = await _enviar_json(
        "contrataciones", f"/contrataciones/{contratacion_id}/check-out", {})
    if contratacion is None:
        raise HTTPException(409, "No se pudo cerrar el servicio: revisa que haya check-in")

    cobro = None
    if contratacion["medioPago"] == "EFECTIVO":
        cobro = await _enviar_json("monetizacion", "/comisiones", {
            "prestadorId": contratacion["prestadorId"],
            "contratacionId": contratacion["id"],
            "monto": contratacion["montoComision"],
            "medioPago": contratacion["medioPago"],
        })

    usuario_dem, usuario_pres = await _usuarios_de(
        contratacion["demandanteId"], contratacion["prestadorId"])
    await asyncio.gather(
        _avisar(usuario_dem, "SERVICIO_COMPLETADO",
                "El servicio terminó. Cuéntanos cómo te fue dejando tu reseña."),
        _avisar(usuario_pres, "COMISION_APLICADA",
                f"Servicio cerrado. Comisión de ${round(contratacion['montoComision']):,} COP "
                f"cargada a tu billetera."),
    )
    return {"contratacion": contratacion, "cobro": cobro}


@app.post("/api/bff/resenas", tags=["bff"], status_code=201,
          summary="Publicar una reseña y refrescar la reputación del perfil")
async def bff_resena(cuerpo: dict):
    resena = await _enviar_json("confianza", "/resenas", cuerpo)
    if resena is None:
        raise HTTPException(503, "El contexto de Confianza no pudo registrar la reseña")

    # El promedio publicado vive en Identidad, pero lo calcula Confianza sobre
    # el detalle que sí es suyo. Se recalcula entero en vez de irlo sumando.
    receptor = resena["receptorId"]
    resumen = await _pedir("confianza", "/resenas/resumen", {"receptorId": receptor})
    perfil = None
    if resumen:
        perfil = await _enviar_json("identidad", f"/prestadores/{receptor}/reputacion", {
            "calificacionPromedio": resumen["promedio"], "totalResenas": resumen["total"],
        })
    return {"resena": resena, "resumen": resumen, "perfil": perfil}


@app.post("/api/bff/prestadores/{prestador_id}/plan", tags=["bff"],
          summary="Cambiar el plan del prestador (perfil + suscripción)")
async def bff_cambiar_plan(prestador_id: str, cuerpo: dict):
    """Un plan vive en dos contextos y los dos tienen que moverse juntos.

    Identidad guarda qué plan muestra el perfil; Monetización, la suscripción
    que lo sostiene. Lo que no cambia es ningún acuerdo ya cerrado: su comisión
    quedó escrita en la contratación.
    """
    plan = str(cuerpo.get("plan", "")).upper()
    if plan not in ("FREE", "PRO"):
        raise HTTPException(422, "Indica 'plan': FREE o PRO")

    perfil = await _enviar_json("identidad", f"/prestadores/{prestador_id}/plan", {"plan": plan})
    if perfil is None:
        raise HTTPException(404, f"Prestador '{prestador_id}' no encontrado en Identidad")

    facturacion = await _enviar_json("monetizacion", "/suscripciones", {
        "prestadorId": prestador_id, "plan": plan,
    }) or {}
    await _avisar(perfil.get("usuarioId"), "PLAN_ACTUALIZADO",
                  f"Tu plan quedó en {plan}. Aplica a los acuerdos que cierres desde ahora.")
    return {
        "perfil": perfil,
        "plan": plan,
        "porcentajeComision": facturacion.get("porcentajeComision"),
        "suscripcion": facturacion.get("suscripcion"),
    }


# --- Proxy genérico --------------------------------------------------------
@app.get("/api/{servicio}/{ruta:path}", tags=["proxy"],
         summary="Reenviar una consulta al microservicio dueño del contexto")
async def proxy(servicio: str, ruta: str, request: Request):
    if servicio not in SERVICIOS:
        raise HTTPException(
            404,
            f"Servicio '{servicio}' desconocido. Consulta /api/servicios.",
        )
    destino = f"{SERVICIOS[servicio]['url']}/{ruta}"
    try:
        respuesta = await _cliente.get(destino, params=dict(request.query_params))
    except httpx.HTTPError as e:
        raise HTTPException(503, f"El servicio '{servicio}' no está disponible: {e}") from e

    tipo = respuesta.headers.get("content-type", "")
    if "application/json" in tipo:
        return JSONResponse(status_code=respuesta.status_code, content=respuesta.json())
    return JSONResponse(
        status_code=respuesta.status_code,
        content={"servicio": servicio, "contenido": respuesta.text},
    )


@app.post("/api/{servicio}/{ruta:path}", tags=["proxy"],
          summary="Reenviar una escritura al microservicio dueño del contexto")
async def proxy_escritura(servicio: str, ruta: str, cuerpo: dict):
    if servicio not in SERVICIOS:
        raise HTTPException(404, f"Servicio '{servicio}' desconocido. Consulta /api/servicios.")
    return await _enviar(servicio, f"/{ruta}", cuerpo)
