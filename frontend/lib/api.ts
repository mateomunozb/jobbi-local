// Cliente del API Gateway de JOBBI.
//
// Todas las llamadas van a `/api/...` en el mismo origen; next.config.mjs las
// reenvía al gateway. El frontend nunca habla directo con un microservicio de
// dominio: si necesita datos de varios contextos usa una ruta /api/bff/*, que
// el gateway compone en una sola respuesta.

export type Rol = "Demandante" | "Prestador" | "Admin"

export type Ubicacion = { municipio: string; comuna: string; barrio: string; latitud: number; longitud: number }
export type Usuario = { id: string; nombreCompleto: string; correo: string; telefono: string; tipoDocumento: string; numeroDocumento: string; fechaRegistro: string; estado: string }
export type PerfilDemandante = { id: string; usuarioId: string; nombreCompleto: string; ubicacionPrincipal: Ubicacion; fechaActivacion: string }
export type PerfilPrestador = { id: string; usuarioId: string; nombreCompleto: string; telefono: string; descripcion: string; portafolioUrl: string; tarifaReferencialBase: number; insigniaVerificado: boolean; estadoVerificacionActual: string; planActual: string; calificacionPromedio: number; totalResenas: number; fechaActivacion: string; ubicacionPrincipal: Ubicacion }
export type Sesion = { usuario: Usuario; rol: Rol; perfilDemandante: PerfilDemandante | null; perfilPrestador: PerfilPrestador | null; verificado: boolean }

export type Categoria = { id: string; nombre: string; descripcion: string }
export type Oficio = { id: string; categoriaId: string; nombre: string; descripcion: string }
export type OfertaOficio = { oficio: Oficio; categoria: Categoria | null; tarifaReferencial: number; anosExperiencia: number }
export type CatalogoBff = { categorias: Categoria[] | null; oficios: Pagina<Oficio> | null; prestadores: Pagina<PerfilPrestador> | null }
export type Contacto = { id: string; demandanteId: string; prestadorId: string; busquedaOrigenId: string | null; fechaInicio: string; estado: string }
export type Contratacion = { id: string; demandanteId: string; prestadorId: string; oficioId: string; fechaSolicitud: string; fechaEjecucion: string | null; estado: string; valorAcordado: number; medioPago: string; porcentajeComisionAplicado: number; montoComision: number; checkIn: string | null; checkOut: string | null }
export type PasoTimeline = { estado: string; alcanzado: boolean }
export type Timeline = { contratacionId: string; estadoActual: string; checkIn: string | null; checkOut: string | null; duracionMinutos: number | null; pasos: PasoTimeline[] }
export type Mensaje = { id: string; conversacionId: string; remitenteId: string; contenido: string; fechaEnvio: string; leido: boolean }
/**
 * Hay un chat por servicio: ABIERTA mientras se negocia y se ejecuta; CERRADA
 * (solo lectura) cuando su servicio se cierra. Contactar de nuevo abre otra.
 */
export type Conversacion = {
  id: string; contactoId: string; fechaInicio: string
  estado: "ABIERTA" | "CERRADA"; creadaEn: string | null; fechaCierre: string | null; contratacionId: string | null
  totalMensajes: number; noLeidos: number; ultimoMensaje: Mensaje | null
}
/** Negociación de la tarifa dentro del chat, previa al servicio. */
export type AcuerdoTarifa = {
  id: string; contactoId: string; conversacionId: string | null
  demandanteId: string; prestadorId: string; oficioId: string
  valorPropuesto: number; medioPago: string; propuestoPor: "DEMANDANTE" | "PRESTADOR"
  aceptadoDemandante: boolean; aceptadoPrestador: boolean; estado: string
  porcentajeComisionCongelado: number | null; planPrestadorAlAcordar: string | null
  contratacionId: string | null; fechaPropuesta: string; fechaCierre: string | null
}
/**
 * Qué se puede hacer todavía con una contratación. Lo calcula el gateway (cruza
 * Contrataciones, Confianza y Soporte) y las pantallas solo lo obedecen: un
 * servicio terminado admite calificar y reportar; al calificarlo el demandante
 * queda cerrado y el chat vuelve a quedar libre para acordar otro.
 */
export type CicloContratacion = {
  terminada: boolean
  cerrada: boolean
  calificadaPor: { DEMANDANTE: boolean; PRESTADOR: boolean }
  puedeCalificar: { DEMANDANTE: boolean; PRESTADOR: boolean }
  incidenteReportado: boolean
  puedeReportarIncidente: boolean
}
/** Conversación ya resuelta por el gateway: con quién se habla y qué se acordó. */
export type ConversacionEnBandeja = Conversacion & {
  contacto: Contacto; otraParteId: string; otraParteNombre: string
  acuerdo: AcuerdoTarifa | null; contratacion: Contratacion | null
  ciclo: CicloContratacion | null
}
export type Notificacion = { id: string; usuarioId: string; tipo: string; canal: string; contenido: string; fechaEnvio: string; leida: boolean }
export type Resena = { id: string; contratacionId: string; autorId: string; receptorId: string; puntuacion: number; comentario: string; fecha: string; estadoModeracion: string }
export type Billetera = { id: string; prestadorId: string; saldoPendiente: number; bloqueada: boolean }
export type Movimiento = { id: string; billeteraId: string; contratacionId: string | null; tipo: string; monto: number; fecha: string }
export type Incidente = { id: string; contratacionId: string; tipo: string; estado: string; evidenciaDescripcion: string; resolucion: string | null; fechaApertura: string; fechaCierre: string | null }

export type Pagina<T> = { total: number; page: number; size: number; pages: number; items: T[] }

// --- Pub/Sub: el cobro de la comisión viaja como evento --------------------
// Contrataciones guarda CONTRATACION_COMPLETADA en su outbox al hacer check-out,
// el relay lo publica en SNS, y Monetización lo consume de su cola SQS y cobra.

/** Etapa del evento de una contratación: outbox → SNS/SQS → billetera. */
export type EtapaCobro = "SIN_EVENTO" | "EN_OUTBOX" | "PUBLICADO" | "COBRADO"
export type EventoOutbox = { id: string; tipo: string; agregadoId: string; estado: "PENDIENTE" | "PUBLICADO"; intentos: number; ultimoError: string | null; fechaCreacion: string; fechaPublicacion: string | null; mensajeSnsId: string | null }
export type EventoProcesado = { eventoId: string; tipo: string; resultado: string; origen: string; monto: number; fechaProcesado: string; latenciaMs: number | null }
export type EstadoCobro = {
  contratacionId: string
  modo: "EVENTOS"
  etapa: EtapaCobro
  evento: EventoOutbox | null
  cobro: { cobrado: boolean; evento: EventoProcesado | null; movimiento: Movimiento | null; billetera: Billetera | null } | null
}
export type Latencias = { muestras: number; promedio: number | null; p95: number | null }
export type Cola = { nombre: string; visibles?: number; enVuelo?: number; error?: string }
export type EstadoPubSub = {
  modo: "EVENTOS"
  productor: {
    total: number; pendientes: number; publicados: number; pendientesConReintentos: number
    latenciaPublicacionMs: Latencias
    relay: { activo: boolean; tema: string; ultimaVuelta: string | null; ultimoError: string | null }
  } | null
  consumidor: {
    workerActivo: boolean; conectado: boolean; hilos: number
    cobrosProcesados: number; porResultado: Record<string, number>
    desdeArranque: { recibidos: number; procesados: number; duplicadosDescartados: number; errores: number }
    latenciaExtremoAExtremoMs: Latencias
    ultimoError: string | null
    colas: { principal: Cola; dlq: Cola } | null
  } | null
}

/** Error con el código HTTP y el mensaje que devolvió el backend. */
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

const enviar = <T>(ruta: string, cuerpo: unknown) =>
  pedir<T>(ruta, { method: "POST", body: JSON.stringify(cuerpo) })

async function pedir<T>(ruta: string, init?: RequestInit): Promise<T> {
  const respuesta = await fetch(`/api${ruta}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  })
  if (!respuesta.ok) {
    // FastAPI responde {"detail": "..."} o, en validaciones, una lista de errores.
    const cuerpo = await respuesta.json().catch(() => null)
    const detalle = cuerpo?.detail
    const mensaje = typeof detalle === "string" ? detalle
      : Array.isArray(detalle) ? detalle.map((e: any) => e.msg).join(". ")
      : `Error ${respuesta.status}`
    throw new ApiError(respuesta.status, mensaje)
  }
  return respuesta.json()
}

const query = (params: Record<string, string | number | boolean | undefined>) => {
  const buscar = new URLSearchParams()
  for (const [clave, valor] of Object.entries(params)) {
    if (valor !== undefined && valor !== "") buscar.append(clave, String(valor))
  }
  const cadena = buscar.toString()
  return cadena ? `?${cadena}` : ""
}

export type RegistroPayload = {
  nombreCompleto: string
  correo: string
  telefono: string
  tipoDocumento: string
  numeroDocumento: string
  rol: "Demandante" | "Prestador"
  municipio?: string
  barrio?: string
  descripcion?: string
  tarifaReferencialBase?: number
}

/** Municipios con cobertura. El backend los traduce a coordenadas. */
export const MUNICIPIOS = ["Medellín", "Envigado", "Bello", "Itagüí", "Sabaneta"]

/** Categorías sugeridas al declarar un oficio; el backend crea la que falte. */
export const CATEGORIAS_SUGERIDAS = ["Hogar", "Belleza", "Bienestar", "Jardinería", "Tecnología", "Mascotas"]

export const api = {
  // --- Autenticación ---
  registro: (datos: RegistroPayload) => enviar<Sesion>("/auth/registro", datos),
  login: (correo: string) => enviar<Sesion>("/auth/login", { correo }),

  // --- Identidad ---
  prestadores: (filtros: { verificado?: boolean; calificacionMinima?: number; municipio?: string; q?: string; orden?: string; size?: number } = {}) =>
    pedir<Pagina<PerfilPrestador>>(`/identidad/prestadores${query(filtros)}`),
  prestador: (id: string) => pedir<PerfilPrestador>(`/identidad/prestadores/${id}`),
  usuario: (id: string) => pedir<Usuario>(`/identidad/usuarios/${id}`),

  // --- Mercado: altas ---
  // El catálogo no viene cargado: lo construyen los prestadores al registrarse.
  altaOficio: (categoriaNombre: string, oficioNombre: string) =>
    enviar<{ categoria: Categoria; oficio: Oficio }>("/mercado/catalogo/oficio", { categoriaNombre, oficioNombre }),
  altaOferta: (datos: { prestadorId: string; oficioId: string; tarifaReferencial: number; anosExperiencia: number }) =>
    enviar<OfertaOficio>("/mercado/prestador-oficios", datos),

  // --- Mercado ---
  categorias: () => pedir<Categoria[]>("/mercado/categorias"),
  oficios: (categoriaId?: string) => pedir<Pagina<Oficio>>(`/mercado/oficios${query({ categoriaId })}`),
  oficiosDePrestador: (id: string) => pedir<{ prestadorId: string; total: number; items: OfertaOficio[] }>(`/mercado/prestadores/${id}/oficios`),
  contactos: (filtros: { demandanteId?: string; prestadorId?: string } = {}) =>
    pedir<Pagina<Contacto>>(`/mercado/contactos${query(filtros)}`),

  // --- Acuerdo de tarifa y ejecución del servicio ---
  // Todas pasan por el gateway: el porcentaje de comisión depende del plan
  // (Identidad) y de la tarifa vigente (Monetización), y el cobro escribe en
  // la billetera. Ningún contexto resuelve solo ninguno de estos pasos.
  proponerTarifa: (datos: { contactoId: string; conversacionId: string; demandanteId: string; prestadorId: string; valorPropuesto: number; medioPago: string; propuestoPor: "DEMANDANTE" | "PRESTADOR"; oficioId?: string }) =>
    enviar<{ acuerdo: AcuerdoTarifa; comisionVigente: { plan: string; porcentaje: number } }>("/bff/acuerdos", datos),
  aceptarTarifa: (acuerdoId: string, rol: "DEMANDANTE" | "PRESTADOR") =>
    enviar<{ acuerdo: AcuerdoTarifa; contratacion: Contratacion | null }>(`/bff/acuerdos/${acuerdoId}/aceptar`, { rol }),
  checkIn: (contratacionId: string) =>
    enviar<{ contratacion: Contratacion }>(`/bff/contrataciones/${contratacionId}/check-in`, {}),
  // El cobro es siempre asíncrono (`cobro` llega null): la comisión se carga
  // cuando Monetización consume el evento, y se sigue con `estadoCobro`.
  checkOut: (contratacionId: string) =>
    enviar<{ contratacion: Contratacion; cobro: null; cobroAsincrono: true }>(`/bff/contrataciones/${contratacionId}/check-out`, {}),
  estadoCobro: (contratacionId: string) =>
    pedir<EstadoCobro>(`/bff/contrataciones/${contratacionId}/cobro`),
  estadoPubSub: () => pedir<EstadoPubSub>("/bff/pubsub/estado"),
  publicarResena: (datos: { contratacionId: string; autorId: string; receptorId: string; puntuacion: number; comentario: string }) =>
    enviar<{ resena: Resena; resumen: { promedio: number; total: number } | null }>("/bff/resenas", datos),
  cambiarPlan: (prestadorId: string, plan: "FREE" | "PRO") =>
    enviar<{ perfil: PerfilPrestador; plan: string; porcentajeComision: number | null; suscripcion: any }>(`/bff/prestadores/${prestadorId}/plan`, { plan }),
  planes: () =>
    pedir<{ total: number; items: { plan: string; porcentajeComision: number; valorMensual: number }[]; umbralBloqueoBilletera: number }>("/monetizacion/planes"),
  comisionVigente: (prestadorId: string) =>
    pedir<{ prestadorId: string; plan: string; porcentajeComision: number }>(`/bff/comision-vigente/${prestadorId}`),

  // --- Contrataciones ---
  contrataciones: (filtros: { demandanteId?: string; prestadorId?: string; estado?: string; size?: number } = {}) =>
    pedir<Pagina<Contratacion>>(`/contrataciones/contrataciones${query(filtros)}`),
  contratacion: (id: string) => pedir<Contratacion>(`/contrataciones/contrataciones/${id}`),
  timeline: (id: string) => pedir<Timeline>(`/contrataciones/contrataciones/${id}/timeline`),
  resumenContrataciones: (filtros: { prestadorId?: string; demandanteId?: string } = {}) =>
    pedir<{ total: number; porEstado: Record<string, number>; totalCompletadas: number; valorTotalCompletado: number; comisionTotalCompletada: number; ticketPromedio: number }>(`/contrataciones/resumen${query(filtros)}`),

  // --- Comunicación: bandeja y envío ---
  // La bandeja la compone el gateway (contactos + conversaciones + nombres),
  // porque ningún contexto por sí solo sabe con quién se está hablando.
  bandeja: (filtros: { demandanteId?: string; prestadorId?: string }) =>
    pedir<{ total: number; items: ConversacionEnBandeja[] }>(`/bff/mensajes${query(filtros)}`),
  // busquedaOrigenId: la búsqueda de la que salió el prestador, si la hubo
  // (así se mide la tasa de contacto tras búsqueda).
  contactar: (demandanteId: string, prestadorId: string, busquedaOrigenId?: string) =>
    enviar<{ contacto: Contacto; conversacion: Conversacion }>("/bff/contactar", { demandanteId, prestadorId, busquedaOrigenId }),
  enviarMensaje: (conversacionId: string, remitenteId: string, contenido: string) =>
    enviar<Mensaje>("/comunicacion/mensajes", { conversacionId, remitenteId, contenido }),
  // Pasa por el gateway, que rechaza el reporte si el servicio ya está cerrado.
  reportarIncidente: (contratacionId: string, tipo: string, evidenciaDescripcion: string) =>
    enviar<Incidente>("/bff/incidentes", { contratacionId, tipo, evidenciaDescripcion }),

  // --- Comunicación ---
  conversaciones: (filtros: { contactoId?: string; participanteId?: string } = {}) =>
    pedir<Pagina<Conversacion>>(`/comunicacion/conversaciones${query(filtros)}`),
  mensajes: (conversacionId: string) =>
    pedir<Pagina<Mensaje>>(`/comunicacion/conversaciones/${conversacionId}/mensajes?size=200`),
  notificaciones: (usuarioId: string) =>
    pedir<Pagina<Notificacion>>(`/comunicacion/notificaciones${query({ usuarioId })}`),
  resumenNotificaciones: (usuarioId: string) =>
    pedir<{ usuarioId: string; total: number; noLeidas: number; noLeidasPorCanal: Record<string, number> }>(`/comunicacion/notificaciones/resumen${query({ usuarioId })}`),

  // --- Confianza ---
  resenas: (filtros: { receptorId?: string; contratacionId?: string } = {}) =>
    pedir<Pagina<Resena>>(`/confianza/resenas${query(filtros)}`),
  resumenResenas: (receptorId: string) =>
    pedir<{ receptorId: string; total: number; promedio: number; distribucion: Record<string, number> }>(`/confianza/resenas/resumen${query({ receptorId })}`),
  estadoVerificacion: (prestadorId: string) =>
    pedir<{ prestadorId: string; totalVerificaciones: number; aprobadas: number; pendientes: number; insigniaVigente: boolean; tiposAprobados: string[] }>(`/confianza/prestadores/${prestadorId}/estado-verificacion`),

  // --- Monetización ---
  billeteras: (prestadorId: string) => pedir<Pagina<Billetera>>(`/monetizacion/billeteras${query({ prestadorId })}`),
  movimientos: (billeteraId: string) => pedir<Pagina<Movimiento>>(`/monetizacion/billeteras/${billeteraId}/movimientos`),
  resumenFinanciero: (prestadorId: string) =>
    pedir<{ prestadorId: string; billetera: Billetera | null; suscripcionActiva: any; totalMovimientos: number; comisionesAcumuladas: number }>(`/monetizacion/resumen/prestador/${prestadorId}`),

  // --- Soporte ---
  incidentes: (filtros: { contratacionId?: string; abiertos?: boolean } = {}) =>
    pedir<Pagina<Incidente>>(`/soporte/incidentes${query(filtros)}`),

  // --- Vistas compuestas (BFF) ---
  bffPrestador: (id: string) => pedir<{
    perfil: PerfilPrestador
    oficios: { total: number; items: OfertaOficio[] } | null
    verificacion: { insigniaVigente: boolean; tiposAprobados: string[] } | null
    ultimasResenas: Pagina<Resena> | null
    contrataciones: { total: number; totalCompletadas: number } | null
    finanzas: { comisionesAcumuladas: number } | null
  }>(`/bff/prestadores/${id}`),
  bffContratacion: (id: string) => pedir<{
    contratacion: Contratacion
    timeline: Timeline | null
    demandante: PerfilDemandante | null
    prestador: PerfilPrestador | null
    oficio: Oficio | null
    pagos: Pagina<any> | null
    resenas: Pagina<Resena> | null
    incidentes: Pagina<Incidente> | null
    ciclo: CicloContratacion
  }>(`/bff/contrataciones/${id}`),
  bffInicioDemandante: (id: string) => pedir<{
    perfil: PerfilDemandante
    contactos: Pagina<Contacto> | null
    ultimasContrataciones: Pagina<Contratacion> | null
    notificaciones: { noLeidas: number } | null
  }>(`/bff/demandantes/${id}/inicio`),
  bffCatalogo: (filtros: { categoriaId?: string; calificacionMinima?: number; municipio?: string } = {}) =>
    pedir<CatalogoBff>(`/bff/catalogo${query(filtros)}`),
  // Lo mismo que el catálogo, pero deja registrada la búsqueda del demandante.
  buscar: (demandanteId: string, filtros: { categoriaId?: string; calificacionMinima?: number; municipio?: string } = {}) =>
    enviar<CatalogoBff & { busqueda: { id: string } | null }>("/bff/busquedas", { demandanteId, ...filtros }),
  bffMetricasAdmin: () => pedir<{
    contrataciones: { total: number; porEstado: Record<string, number>; valorTotalCompletado: number; comisionTotalCompletada: number } | null
    catalogo: { items: { categoria: Categoria; totalOficios: number; totalOfertas: number }[] } | null
    incidentes: { total: number; abiertos: number; porEstado: Record<string, number>; porTipo: Record<string, number> } | null
    canalAdquisicion: { items: { aliado: { nombre: string }; totalReferidos: number; activados: number; tasaActivacion: number }[] } | null
    resenasEnModeracion: number
    totalPrestadores: number
  }>("/bff/admin/metricas"),
}

/** Formatea un valor en pesos colombianos, como los muestra el prototipo. */
export const pesos = (valor: number) => `$${Math.round(valor).toLocaleString("es-CO")}`

/** Iniciales para los avatares, a partir del nombre completo. */
export const iniciales = (nombre: string) =>
  nombre.trim().split(/\s+/).slice(0, 2).map(p => p[0]?.toUpperCase() ?? "").join("")
