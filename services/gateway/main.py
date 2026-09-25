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
import json
from typing import Any

import httpx
import websockets
from fastapi import HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse

from common import eventos
from common.cobertura import en_cobertura
from common.observabilidad import cabeceras_de_traza, log, muestras_de_colas, registrar_metricas
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
# Cada petición del frontend se abre en varias hacia los servicios (una vista BFF
# llega a seis en paralelo). Con el límite por defecto de httpx (20 conexiones
# reutilizables) el resto se abre y se cierra en cada llamada: bajo carga eso
# satura la cola de aceptación TCP de los servicios y aparecen ConnectError.
# Reutilizarlas lo evita. La expiración queda por debajo de los 5 s que uvicorn
# mantiene viva una conexión ociosa, para no reutilizar una que el servidor ya
# está cerrando.
LIMITES = httpx.Limits(max_connections=400, max_keepalive_connections=400, keepalive_expiry=4.0)
_cliente: httpx.AsyncClient | None = None

# El cobro de comisiones es siempre asíncrono (ADR-002): el check-out no cobra,
# Contrataciones publica CONTRATACION_COMPLETADA y Monetización cobra al
# consumirlo. No existe un camino síncrono alternativo que se salte el outbox.

# Profundidad de todas las colas SQS. Cada consumidor ya exporta las suyas, pero
# si su Pod muere (Fallo 2) la serie se corta justo cuando la cola se llena; el
# gateway no depende de ellos y la sigue midiendo.
registrar_metricas(lambda: muestras_de_colas({
    "monetizacion": eventos.QUEUE_NAME, "monetizacion-dlq": eventos.DLQ_NAME,
    "comunicacion": eventos.QUEUE_COMUNICACION, "comunicacion-dlq": eventos.DLQ_COMUNICACION}))


@app.on_event("startup")
async def _abrir_cliente() -> None:
    global _cliente
    _cliente = httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITES,
                                 event_hooks={"request": [_propagar_traza]})


async def _propagar_traza(peticion: httpx.Request) -> None:
    # Cada servicio registra sus logs con el mismo traceId que el gateway.
    peticion.headers.update(cabeceras_de_traza())


@app.on_event("shutdown")
async def _cerrar_cliente() -> None:
    if _cliente:
        await _cliente.aclose()


async def _pedir(servicio: str, ruta: str, params: dict | None = None) -> Any:
    """Llama a un servicio de dominio. Devuelve None si el contexto no responde."""
    base = SERVICIOS[servicio]["url"]
    try:
        respuesta = await _cliente.get(f"{base}{ruta}", params=params)
    except httpx.HTTPError as e:
        log.warning("servicio_no_responde", extra={
            "destino": servicio, "method": "GET", "route": ruta, "error": f"{type(e).__name__}: {e}"})
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


async def _escribir(servicio: str, ruta: str, cuerpo: dict) -> dict:
    """POST al servicio dueño; traduce su fallo en el código correcto para el cliente.

    Un 4xx del servicio es una regla de negocio (409 con su detalle); un 5xx o
    la falta de respuesta es indisponibilidad (503, reintentable). Mezclarlos
    haría que una base caída pareciera un error del usuario.
    """
    base = SERVICIOS[servicio]["url"]
    try:
        respuesta = await _cliente.post(f"{base}{ruta}", json=cuerpo)
    except httpx.HTTPError as e:
        raise HTTPException(503, f"El servicio '{servicio}' no está disponible") from e
    if respuesta.status_code >= 500:
        raise HTTPException(503, f"El servicio '{servicio}' no está disponible en este momento")
    if respuesta.status_code >= 400:
        detalle = respuesta.json().get("detail") if "json" in respuesta.headers.get("content-type", "") else None
        raise HTTPException(409, detalle or f"'{servicio}' rechazó la operación")
    return respuesta.json()


async def _enviar_json(servicio: str, ruta: str, cuerpo: dict) -> Any:
    """Como `_enviar`, pero devuelve el cuerpo ya decodificado, o None si falla.

    Lo usan las composiciones BFF, que encadenan varias altas y necesitan el
    resultado de una para construir la siguiente.
    """
    base = SERVICIOS[servicio]["url"]
    try:
        respuesta = await _cliente.post(f"{base}{ruta}", json=cuerpo)
    except httpx.HTTPError as e:
        log.warning("servicio_no_responde", extra={
            "destino": servicio, "method": "POST", "route": ruta, "error": f"{type(e).__name__}: {e}"})
        return None
    if respuesta.status_code >= 400:
        return None
    return respuesta.json()


# --- Autenticación ---------------------------------------------------------
@app.post("/api/auth/registro", tags=["auth"], status_code=201,
          summary="Registrar un usuario y activar su perfil")
async def registro(cuerpo: dict):
    """Crea la cuenta y, si es prestador, lo verifica con el aliado (RN-01).

    El prestador nace PENDIENTE. Confianza consulta al aliado a través del
    Circuit Breaker: si responde, queda APROBADA o RECHAZADA al instante; si
    está caído, el registro termina igual (PENDIENTE) y Confianza lo reintenta
    en segundo plano.
    """
    respuesta = await _enviar("identidad", "/auth/registro", cuerpo)
    if respuesta.status_code != 201:
        return respuesta
    sesion = json.loads(respuesta.body)
    if sesion.get("perfilPrestador"):
        sesion = await _verificar_prestador(sesion, cuerpo.get("numeroDocumento"))
    return JSONResponse(status_code=201, content=sesion)


@app.post("/api/auth/login", tags=["auth"], summary="Iniciar sesión solo con el correo")
async def login(cuerpo: dict):
    respuesta = await _enviar("identidad", "/auth/login", cuerpo)
    if respuesta.status_code != 200:
        return respuesta
    sesion = json.loads(respuesta.body)
    # Un prestador que sigue PENDIENTE (p. ej. Confianza no respondió al
    # registrarse) aprovecha el login para volver a pedir su verificación.
    if (sesion.get("perfilPrestador") or {}).get("estadoVerificacionActual") == "PENDIENTE":
        sesion = await _verificar_prestador(sesion, (sesion.get("usuario") or {}).get("numeroDocumento"))
    return JSONResponse(status_code=200, content=sesion)


async def _verificar_prestador(sesion: dict, documento: str | None) -> dict:
    """Pide la verificación a Confianza y devuelve la sesión con el perfil actualizado."""
    prestador_id = sesion["perfilPrestador"]["id"]
    resultado = await _enviar_json("confianza", f"/prestadores/{prestador_id}/verificacion",
                                   {"documento": documento})
    if resultado is None:
        log.warning("verificacion_no_solicitada", extra={"prestadorId": prestador_id})
        return sesion
    perfil = await _pedir("identidad", f"/prestadores/{prestador_id}")
    if perfil is None:
        return sesion
    return {**sesion, "perfilPrestador": perfil, "verificado": perfil["insigniaVerificado"]}


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


# --- Ciclo de vida de una contratación --------------------------------------
# Terminar el trabajo (check-out) no cierra el servicio: queda la calificación y,
# si algo salió mal, el reporte de incidente. El servicio queda **cerrado** cuando
# el demandante lo califica; desde ahí no se puede volver a calificar ni reportar,
# y el chat vuelve a quedar libre para acordar un servicio nuevo.
#
# La regla cruza tres contextos (el estado es de Contrataciones, las reseñas de
# Confianza y los incidentes de Soporte), así que vive aquí, en un solo lugar:
# las pantallas la leen de `ciclo` y las escrituras la validan antes de pasar.

_TERMINADA = "COMPLETADA"
# Estados en los que tiene sentido reportar un problema: desde que el servicio
# se acordó (p. ej. el prestador no llega) hasta que termina.
_REPORTABLES = {"SOLICITADA", "ACEPTADA", "EN_CURSO", "CHECK_IN", "CHECK_OUT", "COMPLETADA"}


def _ciclo_desde(contratacion: dict, resenas: dict | None, incidentes: dict | None) -> dict:
    """Qué se puede hacer todavía con una contratación. Función pura, sin E/S."""
    estado = contratacion["estado"]
    autores = {r["autorId"] for r in (resenas or {}).get("items") or []}
    calificada = {
        "DEMANDANTE": contratacion["demandanteId"] in autores,
        "PRESTADOR": contratacion["prestadorId"] in autores,
    }
    terminada = estado == _TERMINADA
    cerrada = (terminada and calificada["DEMANDANTE"]) or estado == "CANCELADA"
    reportado = ((incidentes or {}).get("total") or 0) > 0
    # Si Confianza o Soporte no respondieron no se sabe si ya se hizo: ante la
    # duda no se ofrece la acción (el servicio dueño la rechazaría igual).
    return {
        "terminada": terminada,
        "cerrada": cerrada,
        "calificadaPor": calificada,
        "puedeCalificar": {
            rol: resenas is not None and terminada and not hecho
            for rol, hecho in calificada.items()
        },
        "incidenteReportado": reportado,
        "puedeReportarIncidente": (
            incidentes is not None and resenas is not None
            and estado in _REPORTABLES and not cerrada and not reportado
        ),
    }


async def _ciclo(contratacion: dict) -> dict:
    resenas, incidentes = await asyncio.gather(
        _pedir("confianza", "/resenas", {"contratacionId": contratacion["id"], "size": 10}),
        _pedir("soporte", "/incidentes", {"contratacionId": contratacion["id"], "size": 1}),
    )
    return _ciclo_desde(contratacion, resenas, incidentes)


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
        "ciclo": _ciclo_desde(contratacion, resenas, incidentes),
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
    # RN-01: la búsqueda solo muestra prestadores verificados (APROBADA); los
    # PENDIENTE y RECHAZADOS no aparecen.
    filtros_prestador = {"orden": "calificacion", "size": 20, "estadoVerificacion": "APROBADA"}
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


@app.post("/api/bff/busquedas", tags=["bff"], status_code=201,
          summary="Buscar prestadores y registrar la búsqueda (origen del match rate)")
async def bff_buscar(cuerpo: dict):
    """El catálogo, más la constancia de que el demandante buscó.

    La búsqueda se registra en Mercado con los filtros y la ubicación del
    demandante; su id vuelve con los resultados para que el contacto que salga
    de ellos la cite (`busquedaOrigenId`). Si Mercado no puede registrarla, los
    resultados se devuelven igual: la métrica no puede tumbar la búsqueda.
    """
    demandante_id = cuerpo.get("demandanteId")
    if not demandante_id:
        raise HTTPException(422, "Se requiere 'demandanteId'")
    perfil = await _pedir("identidad", f"/demandantes/{demandante_id}")
    if perfil is None:
        raise HTTPException(404, f"Demandante '{demandante_id}' no encontrado")

    ubicacion = perfil.get("ubicacionPrincipal") or {}
    categoria_id = cuerpo.get("categoriaId")
    calificacion = cuerpo.get("calificacionMinima")
    municipio = cuerpo.get("municipio")
    busqueda, catalogo = await asyncio.gather(
        _enviar_json("mercado", "/busquedas", {
            "demandanteId": demandante_id,
            "categoriaId": categoria_id,
            "calificacionMinima": calificacion,
            "municipio": municipio or ubicacion.get("municipio") or "Medellín",
            "comuna": ubicacion.get("comuna"),
            "barrio": ubicacion.get("barrio"),
            "latitud": ubicacion.get("latitud"),
            "longitud": ubicacion.get("longitud"),
        }),
        bff_catalogo(categoriaId=categoria_id, calificacionMinima=calificacion, municipio=municipio),
    )
    return {**catalogo, "busqueda": busqueda}


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

    # Hay un chat por servicio, y en cada uno se negocia la tarifa de ese
    # servicio: la bandeja trae el acuerdo de cada chat, sin el cual la pantalla
    # no sabría si mostrar proponer, aceptar, el servicio en curso o el cierre.
    chats = (conversaciones or {}).get("items") or []
    acuerdos = await _pedir("contrataciones", "/acuerdos", {
        "contactoIds": ",".join(por_id), "size": 200,
    })
    # Los acuerdos de antes del chat por servicio no traen conversación: se
    # atribuyen al chat más antiguo de su contacto, que es donde nacieron.
    mas_antiguo: dict[str, str] = {}
    for chat in reversed(chats):
        mas_antiguo.setdefault(chat["contactoId"], chat["id"])
    vigente_por_chat: dict[str, dict] = {}
    for acuerdo in (acuerdos or {}).get("items") or []:
        # Vienen del más reciente al más antiguo: el primero útil de cada chat
        # es el que manda.
        if acuerdo["estado"] not in ("PENDIENTE", "ACEPTADO"):
            continue
        chat_id = acuerdo.get("conversacionId") or mas_antiguo.get(acuerdo["contactoId"])
        vigente_por_chat.setdefault(chat_id, acuerdo)

    contrataciones = await asyncio.gather(*(
        _pedir("contrataciones", f"/contrataciones/{a['contratacionId']}")
        for a in vigente_por_chat.values() if a.get("contratacionId")
    ))
    por_contratacion = {c["id"]: c for c in contrataciones if c}
    ciclos = dict(zip(por_contratacion, await asyncio.gather(
        *(_ciclo(c) for c in por_contratacion.values())
    )))

    items = []
    for chat in chats:
        contacto = por_id.get(chat["contactoId"])
        if contacto is None:
            continue
        otro = contacto["prestadorId"] if soy_demandante else contacto["demandanteId"]
        acuerdo = vigente_por_chat.get(chat["id"])
        ciclo = ciclos.get((acuerdo or {}).get("contratacionId"))
        if chat.get("estado", "ABIERTA") == "ABIERTA" and ciclo and ciclo["cerrada"]:
            # Un chat de antes de este cambio cuyo servicio ya se cerró: se
            # cierra ahora, igual que se habría cerrado al calificar.
            cerrado = await _enviar_json("comunicacion", f"/conversaciones/{chat['id']}/cerrar",
                                         {"contratacionId": acuerdo["contratacionId"]})
            if cerrado:
                chat = {**chat, **cerrado}
        items.append({
            **chat,
            "contacto": contacto,
            "otraParteId": otro,
            "otraParteNombre": nombres.get(otro, "Usuario"),
            "acuerdo": acuerdo,
            "contratacion": por_contratacion.get((acuerdo or {}).get("contratacionId")),
            "ciclo": ciclo,
        })
    # Primero los chats abiertos; los cerrados quedan como historial.
    items.sort(key=lambda item: item.get("estado") == "CERRADA")
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

    # Es la primera señal de una solicitud (o de un cliente que vuelve): el
    # prestador se entera en vivo, sin esperar a que le escriban.
    demandante, (_, usuario_pres) = await asyncio.gather(
        _pedir("identidad", f"/demandantes/{demandante_id}"),
        _usuarios_de(demandante_id, prestador_id),
    )
    await _avisar(usuario_pres, "NUEVO_CONTACTO",
                  f"{(demandante or {}).get('nombreCompleto', 'Un cliente')} quiere contratarte. "
                  "Respóndele en el chat.")
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


async def _exigir_billetera_al_dia(prestador_id: str) -> None:
    """RN-04: un prestador con la billetera bloqueada no acuerda servicios nuevos.

    El bloqueo lo decide Monetización (EspecificacionPrestadorBloqueado); aquí
    solo se lee. Si Monetización no responde se deja pasar: un contexto caído no
    puede paralizar el mercado, y el saldo se sigue cobrando por eventos cuando
    vuelva.
    """
    billeteras = await _pedir("monetizacion", "/billeteras", {"prestadorId": prestador_id, "size": 1})
    billetera = ((billeteras or {}).get("items") or [None])[0]
    if billetera and billetera.get("bloqueada"):
        raise HTTPException(
            409, f"El prestador tiene la billetera bloqueada por comisiones pendientes "
                 f"(${round(billetera['saldoPendiente']):,} COP): debe liquidarlas antes de "
                 "acordar un servicio nuevo")


async def _exigir_prestador_verificado(prestador_id: str) -> dict | None:
    """RN-01: solo un prestador APROBADO puede acordar servicios.

    Lee el estado que Confianza publicó en el perfil (Identidad): aquí no se
    llama al aliado externo. Un prestador PENDIENTE (el aliado estaba caído al
    registrarse) o RECHAZADO no cierra acuerdos, y tampoco aparece en la
    búsqueda. Si Identidad no responde no se puede saber: se deja pasar y se
    registra, porque un contexto caído no puede detener el mercado.
    """
    perfil = await _pedir("identidad", f"/prestadores/{prestador_id}")
    estado = (perfil or {}).get("estadoVerificacionActual")
    if estado == "RECHAZADA":
        raise HTTPException(409, "El prestador no superó la verificación de identidad y antecedentes "
                                 "(RN-01): no puede acordar servicios")
    if estado == "PENDIENTE":
        raise HTTPException(409, "El prestador tiene la verificación de identidad pendiente (RN-01): "
                                 "todavía no puede acordar servicios")
    if perfil is None:
        log.warning("verificacion_desconocida", extra={"prestadorId": prestador_id})
    return {"estado": estado}


async def _exigir_cobertura(demandante_id: str, prestador_id: str) -> None:
    """RN-09: toda contratación de la Fase 1 ocurre dentro del Valle de Aburrá.

    El municipio de cada parte vive en su perfil (Identidad). Si Identidad no
    responde se deja pasar, como en las demás reglas: el registro ya validó la
    cobertura, así que un perfil fuera de ella solo existiría por datos viejos.
    """
    demandante, prestador = await asyncio.gather(
        _pedir("identidad", f"/demandantes/{demandante_id}"),
        _pedir("identidad", f"/prestadores/{prestador_id}"),
    )
    for rol, perfil in (("demandante", demandante), ("prestador", prestador)):
        municipio = ((perfil or {}).get("ubicacionPrincipal") or {}).get("municipio")
        if perfil is not None and not en_cobertura(municipio):
            raise HTTPException(409, f"El {rol} está en '{municipio}', fuera del Valle de Aburrá (RN-09): "
                                     "la Fase 1 no admite contrataciones allí")


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

    chat_id = cuerpo.get("conversacionId")
    if chat_id:
        chat = await _pedir("comunicacion", f"/conversaciones/{chat_id}")
        if chat and chat.get("estado") == "CERRADA":
            raise HTTPException(409, "Este chat se cerró con su servicio: inicia uno nuevo para "
                                     "acordar otro servicio")

    # Un servicio nuevo solo cuando el anterior con esta persona está cerrado.
    # Contrataciones ya impide negociar con uno en curso; lo que solo se ve
    # desde aquí es el que terminó pero aún no se ha calificado.
    previos = await _pedir("contrataciones", "/acuerdos",
                           {"contactoId": contacto_id, "estado": "ACEPTADO", "size": 1})
    ultimo = ((previos or {}).get("items") or [None])[0]
    if ultimo and ultimo.get("contratacionId"):
        anterior = await _pedir("contrataciones", f"/contrataciones/{ultimo['contratacionId']}")
        if anterior and not (await _ciclo(anterior))["cerrada"]:
            raise HTTPException(409, "El servicio anterior con esta persona sigue abierto: "
                                     "termínalo y califícalo antes de acordar uno nuevo")
    await _exigir_billetera_al_dia(prestador_id)
    await _exigir_prestador_verificado(prestador_id)
    await _exigir_cobertura(demandante_id, prestador_id)

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
    # La propuesta pudo hacerse antes del bloqueo: se revisa otra vez al aceptar.
    verificacion = None
    if acuerdo_previo["estado"] == "PENDIENTE":
        await _exigir_billetera_al_dia(acuerdo_previo["prestadorId"])
        verificacion = await _exigir_prestador_verificado(acuerdo_previo["prestadorId"])

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
    return {**resultado, "verificacion": verificacion}


@app.post("/api/bff/contrataciones/{contratacion_id}/check-in", tags=["bff"],
          summary="Check-in del prestador")
async def bff_check_in(contratacion_id: str, cuerpo: dict | None = None):
    contratacion = await _escribir("contrataciones", f"/contrataciones/{contratacion_id}/check-in", {})

    usuario_dem, _ = await _usuarios_de(
        contratacion["demandanteId"], contratacion["prestadorId"])
    await _avisar(usuario_dem, "SERVICIO_INICIADO",
                  "Tu prestador hizo check-in: el servicio está en curso.")
    return {"contratacion": contratacion}


@app.post("/api/bff/contrataciones/{contratacion_id}/check-out", tags=["bff"],
          summary="Check-out del prestador: cierra el servicio y dispara el cobro")
async def bff_check_out(contratacion_id: str, cuerpo: dict | None = None):
    """El cierre del servicio es también el momento del cobro.

    Con pago en efectivo el dinero no pasa por la plataforma: lo que queda es
    cargarle al prestador la comisión pactada en su billetera.

    Ese cobro ya **no** lo hace el gateway. Contrataciones guarda el evento
    CONTRATACION_COMPLETADA junto con el cierre (outbox) y lo publica en SNS;
    Monetización lo consume de su cola SQS y cobra. Así el check-out responde
    sin esperar a Monetización, y funciona aunque Monetización esté caída: el
    cobro llega cuando vuelva. El avance se sigue en
    `/api/bff/contrataciones/{id}/cobro`.
    """
    contratacion = await _escribir("contrataciones", f"/contrataciones/{contratacion_id}/check-out", {})

    monto = f"${round(contratacion['montoComision']):,} COP"
    usuario_dem, usuario_pres = await _usuarios_de(
        contratacion["demandanteId"], contratacion["prestadorId"])
    await asyncio.gather(
        _avisar(usuario_dem, "SERVICIO_COMPLETADO",
                "El servicio terminó. Cuéntanos cómo te fue dejando tu reseña."),
        _avisar(usuario_pres, "COMISION_APLICADA",
                f"Servicio cerrado. La comisión de {monto} se cargará a tu billetera."),
    )
    # `cobro` y `cobroAsincrono` se conservan en la respuesta por compatibilidad
    # con los clientes; el cobro llega siempre por el evento.
    return {"contratacion": contratacion, "cobro": None, "cobroAsincrono": True}


@app.get("/api/bff/contrataciones/{contratacion_id}/cobro", tags=["bff"],
         summary="Seguimiento del cobro asíncrono: outbox → SNS → SQS → billetera")
async def bff_estado_cobro(contratacion_id: str):
    """Junta lo que sabe cada lado del Pub/Sub sobre una contratación.

    Contrataciones sabe si el evento se guardó y si ya salió hacia SNS;
    Monetización sabe si lo consumió y cobró. Ninguno conoce la parte del otro.
    """
    outbox, cobro = await asyncio.gather(
        _pedir("contrataciones", "/outbox", {"agregadoId": contratacion_id, "size": 1}),
        _pedir("monetizacion", f"/cobros/contratacion/{contratacion_id}"),
    )
    evento = ((outbox or {}).get("items") or [None])[0]
    cobrado = bool((cobro or {}).get("cobrado"))
    publicado = bool(evento and evento["estado"] == "PUBLICADO")

    if cobrado:
        etapa = "COBRADO"
    elif publicado:
        etapa = "PUBLICADO"  # en SNS/SQS, esperando al worker
    elif evento:
        etapa = "EN_OUTBOX"  # guardado, esperando al relay
    else:
        etapa = "SIN_EVENTO"
    return {
        "contratacionId": contratacion_id,
        "modo": "EVENTOS",
        "etapa": etapa,
        "evento": evento,
        "cobro": cobro,
    }


@app.get("/api/bff/pubsub/estado", tags=["bff"],
         summary="Tablero del Pub/Sub: productor, colas y consumidor")
async def bff_estado_pubsub():
    productor, consumidor = await asyncio.gather(
        _pedir("contrataciones", "/outbox/resumen"),
        _pedir("monetizacion", "/cobros/estado-worker"),
    )
    return {
        "modo": "EVENTOS",
        "productor": productor,
        "consumidor": consumidor,
    }


@app.post("/api/bff/resenas", tags=["bff"], status_code=201,
          summary="Publicar una reseña y refrescar la reputación del perfil")
async def bff_resena(cuerpo: dict):
    contratacion = await _pedir("contrataciones", f"/contrataciones/{cuerpo.get('contratacionId')}")
    if contratacion is None:
        raise HTTPException(404, "Contratación no encontrada")

    # Quién califica a quién sale de la contratación, no de lo que mande el
    # cliente: cada parte solo puede calificar a la otra.
    autor = cuerpo.get("autorId")
    if autor == contratacion["demandanteId"]:
        rol, receptor = "DEMANDANTE", contratacion["prestadorId"]
    elif autor == contratacion["prestadorId"]:
        rol, receptor = "PRESTADOR", contratacion["demandanteId"]
    else:
        raise HTTPException(403, "Solo las partes de la contratación pueden calificarla")

    ciclo = await _ciclo(contratacion)
    if not ciclo["terminada"]:
        raise HTTPException(409, "Solo se puede calificar un servicio terminado")
    if ciclo["calificadaPor"][rol]:
        raise HTTPException(409, "Ya calificaste este servicio")

    respuesta = await _enviar("confianza", "/resenas", {**cuerpo, "receptorId": receptor})
    if respuesta.status_code != 201:
        return respuesta
    resena = json.loads(respuesta.body)

    # El promedio publicado vive en Identidad, pero lo calcula Confianza sobre
    # el detalle que sí es suyo. Se recalcula entero en vez de irlo sumando.
    # Solo los prestadores tienen reputación pública.
    resumen = perfil = None
    if rol == "DEMANDANTE":
        resumen = await _pedir("confianza", "/resenas/resumen", {"receptorId": receptor})
        if resumen:
            perfil = await _enviar_json("identidad", f"/prestadores/{receptor}/reputacion", {
                "calificacionPromedio": resumen["promedio"], "totalResenas": resumen["total"],
            })
        await _cerrar_chat_de(contratacion["id"])
        _, usuario_pres = await _usuarios_de(contratacion["demandanteId"], receptor)
        await _avisar(usuario_pres, "SERVICIO_CERRADO",
                      f"Tu cliente calificó el servicio con {resena['puntuacion']} estrella(s). "
                      "El servicio quedó cerrado.")
    return {"resena": resena, "resumen": resumen, "perfil": perfil}


async def _cerrar_chat_de(contratacion_id: str) -> None:
    """Un servicio cerrado cierra su chat: queda de solo lectura, y quien lo
    tenga abierto lo ve deshabilitarse en el momento (por WebSocket)."""
    acuerdos = await _pedir("contrataciones", "/acuerdos", {"contratacionId": contratacion_id, "size": 1})
    acuerdo = ((acuerdos or {}).get("items") or [None])[0]
    if not acuerdo:
        return
    chat_id = acuerdo.get("conversacionId")
    if not chat_id:
        # Acuerdo de antes del chat por servicio: su chat es el abierto del contacto.
        abiertos = await _pedir("comunicacion", "/conversaciones",
                                {"contactoId": acuerdo["contactoId"], "estado": "ABIERTA", "size": 1})
        chat_id = (((abiertos or {}).get("items") or [{}])[0]).get("id")
    if chat_id:
        await _enviar_json("comunicacion", f"/conversaciones/{chat_id}/cerrar",
                           {"contratacionId": contratacion_id})


@app.post("/api/bff/incidentes", tags=["bff"], status_code=201,
          summary="Reportar un incidente, solo mientras el servicio no esté cerrado")
async def bff_incidente(cuerpo: dict):
    contratacion = await _pedir("contrataciones", f"/contrataciones/{cuerpo.get('contratacionId')}")
    if contratacion is None:
        raise HTTPException(404, "Contratación no encontrada")

    ciclo = await _ciclo(contratacion)
    if ciclo["cerrada"]:
        raise HTTPException(409, "El servicio ya está cerrado: no admite nuevos reportes")
    if ciclo["incidenteReportado"]:
        raise HTTPException(409, "Ya hay un incidente reportado para este servicio")
    if not ciclo["puedeReportarIncidente"]:
        raise HTTPException(409, f"No se puede reportar un incidente con el servicio en "
                                 f"'{contratacion['estado']}'")
    return await _enviar("soporte", "/incidentes", cuerpo)


@app.post("/api/bff/incidentes/{incidente_id}/investigar", tags=["bff"],
          summary="Pasar un incidente a investigación con la evidencia de su contratación")
async def bff_investigar_incidente(incidente_id: str, cuerpo: dict | None = None):
    """RN-10: el check-in/check-out es de Contrataciones y la regla de Soporte.

    Aquí solo se juntan: se lee la contratación del incidente y se le manda a
    Soporte qué quedó registrado, junto con la denuncia formal si la hay.
    """
    incidente = await _pedir("soporte", f"/incidentes/{incidente_id}")
    if incidente is None:
        raise HTTPException(404, f"Incidente '{incidente_id}' no encontrado")
    contratacion = await _pedir("contrataciones", f"/contrataciones/{incidente['contratacionId']}") or {}
    return await _enviar("soporte", f"/incidentes/{incidente_id}/investigar", {
        "checkInRegistrado": contratacion.get("checkIn") is not None,
        "checkOutRegistrado": contratacion.get("checkOut") is not None,
        "denunciaFormal": (cuerpo or {}).get("denunciaFormal"),
    })


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
    # Lista de permitidos, no de prohibidos (ADR-003: el gateway es el punto de
    # control). Solo pasan tal cual las escrituras que no tienen reglas que
    # crucen contextos. Todo lo demás —acuerdos, check-in/out, reseñas, planes,
    # búsquedas, contactos, comisiones— tiene su ruta /api/bff/…, que valida
    # las reglas del dominio (RN-01, RN-04, RN-05/06, ciclo de vida). Reenviarlo
    # directo permitiría, por ejemplo, aceptar un acuerdo con comisión 0 %.
    destino = f"{servicio}/{ruta}".rstrip("/")
    if destino not in _ESCRITURAS_DIRECTAS:
        log.warning("escritura_directa_rechazada", extra={"destino": destino})
        raise HTTPException(403, f"'POST /api/{destino}' no se acepta por el proxy: usa "
                                 f"{_RUTA_BFF.get(destino, 'la ruta /api/bff/… correspondiente')}, "
                                 "que valida las reglas del dominio")
    return await _enviar(servicio, f"/{ruta}", cuerpo)


# Escrituras sin reglas entre contextos: el servicio dueño las valida solo.
_ESCRITURAS_DIRECTAS = {
    "comunicacion/mensajes",        # el chat valida que la conversación siga abierta
    "mercado/catalogo/oficio",      # alta de categoría y oficio en el catálogo
    "mercado/prestador-oficios",    # el prestador publica su oferta
}

# A dónde remitir las escrituras más comunes que llegan por el proxy.
_RUTA_BFF = {
    "confianza/resenas": "POST /api/bff/resenas",
    "soporte/incidentes": "POST /api/bff/incidentes",
    "contrataciones/acuerdos": "POST /api/bff/acuerdos",
    "mercado/contactos": "POST /api/bff/contactar",
    "mercado/busquedas": "POST /api/bff/busquedas",
}


# --- Proxy WebSocket -------------------------------------------------------
# El chat en tiempo real también entra por el gateway: el navegador abre
# /ws/comunicacion/conversaciones/{id} y aquí se conecta al WebSocket del
# servicio dueño y se copian los mensajes en ambos sentidos. Solo se exponen
# los contextos que tienen tiempo real.
_WS_EXPUESTOS = {"comunicacion"}


@app.websocket("/ws/{servicio}/{ruta:path}")
async def proxy_ws(ws: WebSocket, servicio: str, ruta: str):
    # Los rechazos se hacen después de aceptar, para que el código de cierre
    # llegue al navegador (cerrar sin aceptar es un HTTP 403 sin detalle).
    if servicio not in _WS_EXPUESTOS:
        await ws.accept()
        await ws.close(code=4404, reason=f"'{servicio}' no tiene tiempo real")
        return

    destino = SERVICIOS[servicio]["url"].replace("http", "ws", 1) + f"/ws/{ruta}"
    if ws.url.query:
        destino += f"?{ws.url.query}"

    try:
        servicio_ws = await websockets.connect(destino, open_timeout=5)
    except websockets.exceptions.InvalidStatus as e:
        # El servicio rechazó el handshake.
        await ws.accept()
        await ws.close(code=4404 if e.response.status_code in (403, 404) else 1011)
        return
    except (OSError, TimeoutError, websockets.exceptions.WebSocketException):
        # 1013 "inténtalo más tarde": el cliente reintentará solo.
        print(f"[gateway] WS {servicio}/{ruta}: el servicio no responde", flush=True)
        await ws.accept()
        await ws.close(code=1013, reason="Servicio no disponible")
        return

    await ws.accept()

    async def del_cliente() -> None:
        while True:
            await servicio_ws.send(await ws.receive_text())

    async def del_servicio() -> None:
        async for mensaje in servicio_ws:
            await ws.send_text(mensaje if isinstance(mensaje, str) else mensaje.decode())

    tareas = [asyncio.create_task(del_cliente()), asyncio.create_task(del_servicio())]
    try:
        # Cuando cualquiera de los dos lados se va, se cierra el otro.
        await asyncio.wait(tareas, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for tarea in tareas:
            tarea.cancel()
        await servicio_ws.close()
        codigo = servicio_ws.close_code or 1000
        try:
            await ws.close(code=codigo if codigo != 1006 else 1011)
        except RuntimeError:
            pass  # el navegador ya había cerrado
