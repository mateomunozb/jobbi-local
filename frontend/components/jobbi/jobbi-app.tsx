"use client"

import { useEffect, useMemo, useRef, useState } from "react"
import { ArrowLeft, ArrowRight, Bell, BriefcaseBusiness, CalendarDays, Check, ChevronRight, CircleAlert, Clock3, CreditCard, Home, Loader2, LockKeyhole, LogOut, Mail, MapPin, MessageCircle, MoreHorizontal, RefreshCw, Search, Send, ShieldCheck, Sparkles, Star, UserRound, WalletCards } from "lucide-react"
import { navAdmin, navDemandante, navPrestador, navigationTree, type Role, type Screen } from "./data"
import { ApiError, CATEGORIAS_SUGERIDAS, MUNICIPIOS, api, iniciales, pesos, type Categoria, type Cola, type Contratacion, type ConversacionEnBandeja, type EtapaCobro, type Latencias, type Mensaje, type Notificacion, type PerfilPrestador, type Resena, type Sesion } from "@/lib/api"
import { SesionContext, guardarSesion, leerSesionGuardada, perfilIdDe, useSesion, type Seleccion } from "./session"
import { useChatEnVivo } from "./use-chat"
import { EN_VIVO_MS, useDatos, type EstadoCarga } from "./use-data"

const iconFor = (label: string) => ({ Buscar: Search, Mensajes: MessageCircle, Contrataciones: BriefcaseBusiness, Notificaciones: Bell, Perfil: UserRound, Dashboard: Home, Métricas: Home, Alertas: Bell, Solicitudes: BriefcaseBusiness, Billetera: WalletCards }[label] || MoreHorizontal)

// Los colores de avatar rotan de forma estable a partir del id, para que la
// misma persona conserve siempre el mismo color entre pantallas.
const acentos = ["blue", "rose", "green", "purple"] as const
const acentoDe = (id: string) => acentos[[...id].reduce((suma, c) => suma + c.charCodeAt(0), 0) % acentos.length]

const fecha = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleDateString("es-CO", { day: "numeric", month: "short", year: "numeric" }) : "—"
const hora = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString("es-CO", { hour: "numeric", minute: "2-digit" }) : "—"
// Los valores de las enumeraciones llegan en MAYÚSCULA_CON_GUIONES. La mayoría
// se puede formatear mecánicamente; estos no, porque el guion es parte del
// término tal como lo nombra el modelo de dominio.
const ETIQUETAS: Record<string, string> = { CHECK_IN: "Check-in", CHECK_OUT: "Check-out" }
const enTitulo = (valor: string) =>
  ETIQUETAS[valor] ?? valor.charAt(0) + valor.slice(1).toLowerCase().replace(/_/g, " ")
const tonoEstado = (estado: string) => estado === "COMPLETADA" ? "success" : estado === "EN_DISPUTA" || estado === "CANCELADA" ? "danger" : "warning"

function Button({ children, onClick, variant = "primary", disabled = false, className = "", type = "button" }: { children: React.ReactNode; onClick?: () => void; variant?: "primary" | "secondary" | "ghost" | "danger"; disabled?: boolean; className?: string; type?: "button" | "submit" }) {
  return <button type={type} disabled={disabled} onClick={onClick} className={`jobbi-button jobbi-${variant} ${className}`}>{children}</button>
}
function Badge({ children, tone = "neutral" }: { children: React.ReactNode; tone?: "success" | "warning" | "danger" | "neutral" | "brand" }) { return <span className={`jobbi-badge badge-${tone}`}>{children}</span> }
function Verified() { return <Badge tone="success"><ShieldCheck aria-hidden="true" /> Verificado</Badge> }
function Rating({ value, count }: { value: number; count?: number }) { return <span className="rating"><Star aria-hidden="true" fill="currentColor" /> <strong>{value.toFixed(1)}</strong>{count !== undefined && <span>({count})</span>}</span> }
function SkeletonCard() { return <div className="skeleton-card"><div className="skeleton avatar" /><div className="skeleton-lines"><span className="skeleton" /><span className="skeleton short" /><span className="skeleton" /></div></div> }
function PageHeader({ title, subtitle, onBack, action, className = "" }: { title: string; subtitle?: string; onBack?: () => void; action?: React.ReactNode; className?: string }) { return <header className={`page-header ${className}`}>{onBack ? <button className="icon-button" aria-label="Volver" onClick={onBack}><ArrowLeft /></button> : <div className="brand-mark small">J</div>}<div className="header-title"><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>{action || <div className="header-spacer" />}</header> }
function EmptyState({ title, text, action, onClick, error = false }: { title: string; text: string; action?: string; onClick?: () => void; error?: boolean }) { return <div className="empty-state">{error ? <CircleAlert /> : <Search />}<h2>{title}</h2><p>{text}</p>{action && onClick && <Button variant="secondary" onClick={onClick}>{error && <RefreshCw data-icon="inline-start" />}{action}</Button>}</div> }
function AppContent({ children }: { children: React.ReactNode }) { return <main className="app-content">{children}</main> }

/**
 * Resuelve los tres estados de una consulta al backend con la misma apariencia
 * en toda la app: esqueletos mientras carga, un estado de error con reintento,
 * y los datos cuando llegan.
 */
function Consulta<T>({ estado, children, filas = 2 }: { estado: EstadoCarga<T>; children: (datos: T) => React.ReactNode; filas?: number }) {
  if (estado.cargando) return <div className="provider-list">{Array.from({ length: filas }, (_, i) => <SkeletonCard key={i} />)}</div>
  if (estado.error) return <EmptyState title="No pudimos cargar la información" text={estado.error} action="Reintentar" onClick={estado.recargar} error />
  if (!estado.datos) return null
  return <>{children(estado.datos)}</>
}

function Avatar({ nombre, id, large = false }: { nombre: string; id: string; large?: boolean }) {
  return <div className={`avatar avatar-${acentoDe(id)} ${large ? "large" : ""}`}>{iniciales(nombre) || "?"}</div>
}

type TarjetaPrestador = PerfilPrestador & { ofertas?: { oficioId: string; nombre: string; tarifaReferencial: number }[] }

function ProviderCard({ prestador, onClick }: { prestador: TarjetaPrestador; onClick: () => void }) {
  const oficio = prestador.ofertas?.[0]
  return <button className="provider-card" onClick={onClick}>
    <Avatar nombre={prestador.nombreCompleto} id={prestador.id} />
    <div className="provider-main">
      <div className="row-between">
        <div><h3>{prestador.nombreCompleto}</h3><p>{oficio?.nombre ?? prestador.descripcion}</p></div>
        {prestador.insigniaVerificado && <Verified />}
      </div>
      <div className="provider-meta">
        <Rating value={prestador.calificacionPromedio} count={prestador.totalResenas} />
        <span>{pesos(oficio?.tarifaReferencial ?? prestador.tarifaReferencialBase)} / servicio</span>
      </div>
      <span className="distance"><MapPin aria-hidden="true" /> {prestador.ubicacionPrincipal.barrio}, {prestador.ubicacionPrincipal.municipio}</span>
    </div>
    <ChevronRight aria-hidden="true" className="chevron" />
  </button>
}

function BottomNav({ role, screen, setScreen }: { role: Role; screen: Screen; setScreen: (s: Screen) => void }) { const items = role === "Demandante" ? navDemandante : role === "Admin" ? navAdmin : navPrestador; return <nav className="bottom-nav" aria-label="Navegación principal">{items.map(([key, label]) => { const Icon = iconFor(label); return <button key={key} className={screen === key ? "active" : ""} onClick={() => setScreen(key as Screen)}><Icon aria-hidden="true" /><span>{label}</span></button> })}</nav> }

/** Barra de sesión: quién entró, con qué rol, y cómo salir. */
function SessionBar({ role, setRole, setScreen }: { role: Role; setRole: (r: Role) => void; setScreen: (s: Screen) => void }) {
  const { sesion, salir } = useSesion()
  if (!sesion) return null
  return <div className="session-bar">
    <Avatar nombre={sesion.usuario.nombreCompleto} id={sesion.usuario.id} />
    <div className="session-identity">
      <strong>{sesion.usuario.nombreCompleto}</strong>
      <span>{sesion.rol}{sesion.verificado && <> · <ShieldCheck /> Verificado</>}</span>
    </div>
    {/* No hay cuenta de administrador: es una vista de métricas sobre los
        mismos datos, por eso se ofrece como conmutador y no como login. */}
    <button className={role === "Admin" ? "session-admin selected" : "session-admin"}
      onClick={() => { const volver = role === "Admin"; setRole(volver ? sesion.rol : "Admin"); setScreen(volver ? (sesion.rol === "Prestador" ? "dashboard" : "search") : "admin") }}>
      {role === "Admin" ? "Salir de métricas" : "Métricas"}
    </button>
    <button className="session-exit" onClick={salir} aria-label="Cerrar sesión"><LogOut /></button>
  </div>
}

// A qué pantalla lleva cada aviso, según quién lo recibe.
const DESTINO_AVISO: Record<string, { Prestador: Screen; Demandante: Screen }> = {
  NUEVO_CONTACTO: { Prestador: "requests", Demandante: "chat" },
  TARIFA_PROPUESTA: { Prestador: "requests", Demandante: "chat" },
  SERVICIO_CONFIRMADO: { Prestador: "requests", Demandante: "tracking" },
  SERVICIO_INICIADO: { Prestador: "requests", Demandante: "tracking" },
  SERVICIO_COMPLETADO: { Prestador: "requests", Demandante: "tracking" },
  COMISION_APLICADA: { Prestador: "wallet", Demandante: "tracking" },
  SERVICIO_CERRADO: { Prestador: "requests", Demandante: "tracking" },
  PLAN_ACTUALIZADO: { Prestador: "plans", Demandante: "profile" },
}

/**
 * Avisos en vivo, en cualquier pantalla. Las notificaciones ya las escribe el
 * gateway en cada paso del flujo (contacto, tarifa, check-in, cierre, cobro…);
 * aquí se sondean y las que llegan después de entrar se muestran como tarjeta
 * emergente. Las que ya existían al abrir la app no se anuncian.
 */
function AvisosEnVivo({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { sesion } = useSesion()
  const usuarioId = sesion?.usuario.id
  const lista = useDatos(() => api.notificaciones(usuarioId!), [usuarioId], { cadaMs: EN_VIVO_MS })
  const vistos = useRef<Set<string> | null>(null)
  // Uno a la vez: si llegan varios seguidos se muestra el último y cuántos más
  // hubo, para no tapar la pantalla con una pila de tarjetas.
  const [aviso, setAviso] = useState<{ ultimo: Notificacion; otros: number } | null>(null)

  useEffect(() => {
    const items = lista.datos?.items
    if (!items) return
    if (vistos.current === null) {
      vistos.current = new Set(items.map(n => n.id))
      return
    }
    const nuevos = items.filter(n => !vistos.current!.has(n.id))
    if (!nuevos.length) return
    nuevos.forEach(n => vistos.current!.add(n.id))
    setAviso(previo => ({ ultimo: nuevos[0], otros: (previo ? previo.otros + 1 : 0) + nuevos.length - 1 }))
  }, [lista.datos])

  useEffect(() => {
    if (!aviso) return
    const id = setTimeout(() => setAviso(null), 6000)
    return () => clearTimeout(id)
  }, [aviso])

  if (!aviso || !sesion) return null
  const { ultimo, otros } = aviso
  return <div className="avisos-en-vivo" role="status" aria-live="polite">
    <button className="aviso" onClick={() => {
      setAviso(null)
      setScreen(otros > 0 ? "notifications" : DESTINO_AVISO[ultimo.tipo]?.[sesion.rol as "Prestador" | "Demandante"] ?? "notifications")
    }}>
      <Bell /><div>
        <strong>{enTitulo(ultimo.tipo)}</strong><p>{ultimo.contenido}</p>
        {otros > 0 && <small className="aviso-mas">y {otros} aviso{otros > 1 ? "s" : ""} más · toca para verlos</small>}
      </div>
    </button>
  </div>
}

function AppShell({ children, role, setRole, screen, setScreen }: { children: React.ReactNode; role: Role; setRole: (r: Role) => void; screen: Screen; setScreen: (s: Screen) => void }) {
  const { sesion } = useSesion()
  return <div className="app-frame">
    <SessionBar role={role} setRole={setRole} setScreen={setScreen} />
    {/* key: al cambiar de cuenta, los avisos empiezan de cero. */}
    {sesion && <AvisosEnVivo key={sesion.usuario.id} setScreen={setScreen} />}
    {children}
    <BottomNav role={role} screen={screen} setScreen={setScreen} />
  </div>
}

// ---------------------------------------------------------------------------
// Onboarding
// ---------------------------------------------------------------------------

function MapScreen({ setScreen }: { setScreen: (s: Screen) => void }) { return <div className="center-screen"><div className="brand-lockup"><div className="brand-mark">J</div><span>JOBBI</span></div><div className="eyebrow">MAPA DE NAVEGACIÓN</div><h1>Oficios confiables,<br /><em>cerca de ti.</em></h1><p className="lead">Conecta con personas verificadas para resolver lo que necesitas en Medellín y el Valle de Aburrá.</p><div className="map-list">{navigationTree.map((group, index) => <div className="map-group" key={group.title}><div className="map-index">0{index + 1}</div><div><strong>{group.title}</strong><p>{group.items.join("  ·  ")}</p></div></div>)}</div><Button onClick={() => setScreen("landing")}>Explorar JOBBI <ArrowRight data-icon="inline-end" /></Button></div> }

function Landing({ setScreen }: { setScreen: (s: Screen) => void }) {
  return <div className="center-screen landing">
    <div className="brand-lockup"><div className="brand-mark">J</div><span>JOBBI</span></div>
    <div className="landing-art"><Sparkles /><div>Tu próxima solución<br /><strong>empieza aquí.</strong></div><span className="art-dot one" /><span className="art-dot two" /></div>
    <div><div className="eyebrow">EL MARKETPLACE DE OFICIOS</div><h1>Todo resuelto.<br /><em>Sin complicaciones.</em></h1><p className="lead">Encuentra, contrata y confía en profesionales verificados cerca de ti.</p></div>
    <Button onClick={() => setScreen("role")}>Crear una cuenta <ArrowRight data-icon="inline-end" /></Button>
    <Button variant="secondary" onClick={() => setScreen("login")}>Ya tengo cuenta</Button>
    <button className="text-button" onClick={() => setScreen("map")}>Ver mapa de navegación</button>
  </div>
}

function RoleScreen({ setScreen, setRole }: { setScreen: (s: Screen) => void; setRole: (r: Role) => void }) { const [role, setLocal] = useState<Role>("Demandante"); return <div className="center-screen"><PageHeader title="¿Cómo usarás JOBBI?" subtitle="Puedes cambiarlo después" onBack={() => setScreen("landing")} /><div className="role-cards"><button className={role === "Demandante" ? "role-card active" : "role-card"} onClick={() => setLocal("Demandante")}><div className="role-icon"><Search /></div><strong>Soy Demandante</strong><p>Necesito contratar un oficio para mi hogar o negocio.</p><span>Buscar profesionales <ArrowRight /></span></button><button className={role === "Prestador" ? "role-card active" : "role-card"} onClick={() => setLocal("Prestador")}><div className="role-icon peach"><BriefcaseBusiness /></div><strong>Soy Prestador</strong><p>Ofrezco mis servicios y quiero encontrar clientes.</p><span>Ofrecer mis oficios <ArrowRight /></span></button></div><Button onClick={() => { setRole(role); setScreen("register") }}>Continuar <ArrowRight data-icon="inline-end" /></Button><button className="text-button" onClick={() => setScreen("login")}>Ya tengo cuenta, quiero entrar</button></div> }

function Login({ setScreen, entrar }: { setScreen: (s: Screen) => void; entrar: (s: Sesion) => void }) {
  const [correo, setCorreo] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [cargando, setCargando] = useState(false)

  const enviar = async () => {
    const limpio = correo.trim()
    if (!limpio) { setError("Escribe tu correo para continuar."); return }
    setCargando(true); setError(null)
    try {
      entrar(await api.login(limpio))
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 404
          ? "No encontramos una cuenta con ese correo. Revísalo o crea una cuenta nueva."
          : e instanceof ApiError ? e.message
          : "No pudimos conectar con el servidor. Verifica que el backend esté arriba.",
      )
      setCargando(false)
    }
  }

  return <div className="center-screen login-screen">
    <div className="brand-lockup"><div className="brand-mark">J</div><span>JOBBI</span></div>
    <div>
      <div className="eyebrow">BIENVENIDO DE VUELTA</div>
      <h1>Entra a<br /><em>tu cuenta.</em></h1>
      <p className="lead">Te reconocemos por tu correo. Según tu perfil, te llevamos a buscar oficios o a gestionar tus servicios.</p>
    </div>

    <form className="login-form" onSubmit={event => { event.preventDefault(); enviar() }}>
      <label>Correo electrónico
        <div className="input-icon">
          <Mail />
          <input type="email" autoComplete="email" autoFocus placeholder="tu@correo.com" value={correo}
            onChange={event => { setCorreo(event.target.value); setError(null) }}
            aria-invalid={Boolean(error)} aria-describedby={error ? "login-error" : undefined} />
        </div>
      </label>
      {error && <div className="form-error" id="login-error" role="alert"><CircleAlert /> {error}</div>}
      <Button type="submit" disabled={cargando || !correo.trim()}>
        {cargando ? <><Loader2 className="spin" /> Entrando…</> : <>Entrar <ArrowRight data-icon="inline-end" /></>}
      </Button>
    </form>

    <div className="login-demo">
      <span className="eyebrow">PRIMERA VEZ AQUÍ</span>
      <p>JOBBI arranca sin ninguna cuenta: no hay usuarios de ejemplo. Crea la tuya y el catálogo se irá construyendo con lo que registren los prestadores.</p>
      <Button variant="secondary" onClick={() => setScreen("role")}>Crear una cuenta <ArrowRight data-icon="inline-end" /></Button>
    </div>
  </div>
}

function Register({ role, setScreen, entrar }: { role: Role; setScreen: (s: Screen) => void; entrar: (s: Sesion) => void }) {
  const esPrestador = role === "Prestador"
  const [campos, setCampos] = useState({ nombreCompleto: "", correo: "", telefono: "", tipoDocumento: "CC", numeroDocumento: "", municipio: "Medellín", barrio: "", descripcion: "", tarifaReferencialBase: "", oficio: "", categoria: CATEGORIAS_SUGERIDAS[0], anosExperiencia: "" })
  const [error, setError] = useState<string | null>(null)
  const [cargando, setCargando] = useState(false)
  const cambiar = (clave: keyof typeof campos) => (event: { target: { value: string } }) => { setCampos(previo => ({ ...previo, [clave]: event.target.value })); setError(null) }

  const completo = campos.nombreCompleto.trim().length >= 3 && campos.correo.includes("@")
    && campos.telefono.trim().length >= 7 && campos.numeroDocumento.trim().length >= 4
    // Sin catálogo sembrado, el oficio del prestador es lo que lo hace
    // encontrable: se pide en el registro, no después.
    && (!esPrestador || campos.oficio.trim().length >= 3)

  const enviar = async () => {
    setCargando(true); setError(null)
    try {
      const sesion = await api.registro({
        nombreCompleto: campos.nombreCompleto.trim(),
        correo: campos.correo.trim(),
        telefono: campos.telefono.trim(),
        tipoDocumento: campos.tipoDocumento,
        numeroDocumento: campos.numeroDocumento.trim(),
        rol: esPrestador ? "Prestador" : "Demandante",
        municipio: campos.municipio || undefined,
        barrio: campos.barrio || undefined,
        descripcion: esPrestador ? campos.descripcion || undefined : undefined,
        tarifaReferencialBase: esPrestador && campos.tarifaReferencialBase ? Number(campos.tarifaReferencialBase) : undefined,
      })

      if (esPrestador && sesion.perfilPrestador) {
        // El oficio y su categoría se crean si aún no existen: así el catálogo
        // crece con cada registro en vez de venir precargado.
        const { oficio } = await api.altaOficio(campos.categoria, campos.oficio.trim())
        await api.altaOferta({
          prestadorId: sesion.perfilPrestador.id,
          oficioId: oficio.id,
          tarifaReferencial: Number(campos.tarifaReferencialBase) || 0,
          anosExperiencia: Number(campos.anosExperiencia) || 0,
        })
      }
      entrar(sesion)
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "Ya existe una cuenta con ese correo. Inicia sesión en su lugar."
          : e instanceof ApiError ? e.message
          : "No pudimos conectar con el servidor. Verifica que el backend esté arriba.",
      )
      setCargando(false)
    }
  }

  return <div className="center-screen">
    <PageHeader title="Crea tu cuenta" subtitle={esPrestador ? "Perfil de prestador" : "Perfil de demandante"} onBack={() => setScreen("role")} />
    <form className="form-stack" onSubmit={event => { event.preventDefault(); if (completo) enviar() }}>
      <label>Nombre completo<input value={campos.nombreCompleto} onChange={cambiar("nombreCompleto")} placeholder="Ej. Laura Martínez" autoComplete="name" /></label>
      <label>Correo electrónico<input type="email" value={campos.correo} onChange={cambiar("correo")} placeholder="tu@correo.com" autoComplete="email" /></label>
      <label>Número de teléfono<div className="phone-input"><span>+57</span><input value={campos.telefono} onChange={cambiar("telefono")} placeholder="300 000 0000" autoComplete="tel" /></div></label>
      <div className="form-grid">
        <label>Tipo de documento<select value={campos.tipoDocumento} onChange={cambiar("tipoDocumento")}><option>CC</option><option>CE</option><option>PPT</option></select></label>
        <label>Número de documento<input value={campos.numeroDocumento} onChange={cambiar("numeroDocumento")} placeholder="1000000000" /></label>
      </div>
      <div className="form-grid">
        <label>Municipio<select value={campos.municipio} onChange={cambiar("municipio")}>{MUNICIPIOS.map(m => <option key={m}>{m}</option>)}</select></label>
        <label>Barrio<input value={campos.barrio} onChange={cambiar("barrio")} placeholder="Laureles" /></label>
      </div>
      {esPrestador && <>
        <label>¿Qué oficio ofreces?<input value={campos.oficio} onChange={cambiar("oficio")} placeholder="Ej. Plomería y reparaciones" /></label>
        <div className="form-grid">
          <label>Categoría<select value={campos.categoria} onChange={cambiar("categoria")}>{CATEGORIAS_SUGERIDAS.map(c => <option key={c}>{c}</option>)}</select></label>
          <label>Años de experiencia<input type="number" min="0" max="70" value={campos.anosExperiencia} onChange={cambiar("anosExperiencia")} placeholder="5" /></label>
        </div>
        <label>Sobre ti y tus servicios<textarea value={campos.descripcion} onChange={cambiar("descripcion")} placeholder="Cuéntales a tus clientes qué haces y con cuánta experiencia." /></label>
        <label>Tarifa de referencia<div className="input-icon"><span>$</span><input type="number" min="0" value={campos.tarifaReferencialBase} onChange={cambiar("tarifaReferencialBase")} placeholder="85000" /></div></label>
      </>}
      {error && <div className="form-error" role="alert"><CircleAlert /> {error}</div>}

      <div className="verification-note"><ShieldCheck /><p><strong>Verificación de identidad aprobada.</strong><br />En esta fase la validación con Truora se da por superada, así que tu cuenta queda activa de inmediato.</p></div>

      <Button type="submit" disabled={!completo || cargando}>
        {cargando ? <><Loader2 className="spin" /> Creando cuenta…</> : <>Crear cuenta <ArrowRight data-icon="inline-end" /></>}
      </Button>
    </form>
    <p className="fine-print center">Al continuar aceptas nuestros términos y condiciones y política de privacidad.</p>
  </div>
}

function Coverage({ role, setScreen }: { role: Role; setScreen: (s: Screen) => void }) { return <div className="center-screen"><div className="status-illustration warning"><MapPin /></div><div className="eyebrow">COBERTURA ACTUAL</div><h1>Aún no llegamos<br /><em>a tu ciudad.</em></h1><p className="lead">Por ahora JOBBI está disponible en Medellín y el Valle de Aburrá. Te avisaremos cuando lleguemos a tu zona.</p><div className="coverage-card"><div><MapPin /><div><strong>Medellín y Valle de Aburrá</strong><span>Disponible ahora</span></div></div><Check /></div><Button onClick={() => setScreen(role === "Demandante" ? "search" : "dashboard")}>Entrar a JOBBI <ArrowRight data-icon="inline-end" /></Button></div> }

// ---------------------------------------------------------------------------
// Demandante
// ---------------------------------------------------------------------------

function SearchScreen({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { sesion, seleccionar } = useSesion()
  const [categoriaId, setCategoriaId] = useState<string | undefined>(undefined)
  const catalogo = useDatos(() => api.bffCatalogo({ categoriaId }), [categoriaId ?? "todas"])
  const avisos = useDatos(() => api.resumenNotificaciones(sesion!.usuario.id), [sesion?.usuario.id])
  const nombrePila = sesion?.usuario.nombreCompleto.split(" ")[0] ?? ""

  return <AppContent>
    <PageHeader title={`Hola, ${nombrePila}`} subtitle="¿Qué necesitas resolver hoy?"
      action={<button className="icon-button" onClick={() => setScreen("notifications")} aria-label="Notificaciones"><Bell />{(avisos.datos?.noLeidas ?? 0) > 0 && <span className="notification-dot" />}</button>} />
    <div className="search-hero">
      <div className="eyebrow">ENCUENTRA LO QUE NECESITAS</div>
      <h2>Un oficio de confianza,<br /><em>a un clic de distancia.</em></h2>
      <button className="search-field" onClick={() => setScreen("results")}><Search /><span>¿Qué oficio buscas?</span><ArrowRight /></button>
    </div>
    <Consulta estado={catalogo}>{datos => <>
      <section>
        <div className="section-heading"><h2>Categorías</h2></div>
        <div className="category-row">
          <button className={!categoriaId ? "category active" : "category"} onClick={() => setCategoriaId(undefined)}><span className="category-icon c0" />Todos</button>
          {(datos.categorias ?? []).map((c: Categoria, i: number) => (
            <button key={c.id} className={categoriaId === c.id ? "category active" : "category"} onClick={() => setCategoriaId(c.id)}>
              <span className={`category-icon c${(i + 1) % 5}`} />{c.nombre}
            </button>
          ))}
        </div>
      </section>
      <section>
        <div className="section-heading"><h2>Prestadores cerca de ti</h2><button className="text-button" onClick={() => { seleccionar({ categoriaId }); setScreen("results") }}>Ver todos</button></div>
        {(datos.prestadores?.items.length ?? 0) === 0
          ? <EmptyState title="No hay prestadores en esta categoría" text="Prueba con otra categoría o vuelve a Todos." action="Ver todos" onClick={() => setCategoriaId(undefined)} />
          : <div className="provider-list">{(datos.prestadores?.items ?? []).slice(0, 3).map((p: any) => <ProviderCard key={p.id} prestador={p} onClick={() => { seleccionar({ prestadorId: p.id }); setScreen("provider") }} />)}</div>}
      </section>
    </>}</Consulta>
  </AppContent>
}

function Results({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { seleccion, seleccionar } = useSesion()
  const [orden, setOrden] = useState<"calificacion" | "tarifa">("calificacion")
  const catalogo = useDatos(() => api.bffCatalogo({ categoriaId: seleccion.categoriaId }), [seleccion.categoriaId ?? "todas"])

  return <AppContent>
    <PageHeader title="Resultados" subtitle={catalogo.datos ? `${catalogo.datos.prestadores?.total ?? 0} prestadores disponibles` : "Cargando…"} onBack={() => setScreen("search")} />
    <div className="filter-bar">
      <button className={orden === "calificacion" ? "filter active" : "filter"} onClick={() => setOrden("calificacion")}>Mejor calificados</button>
      <button className={orden === "tarifa" ? "filter active" : "filter"} onClick={() => setOrden("tarifa")}>Menor tarifa</button>
    </div>
    <Consulta estado={catalogo} filas={3}>{datos => {
      const lista = [...(datos.prestadores?.items ?? [])].sort((a: any, b: any) =>
        orden === "tarifa" ? a.tarifaReferencialBase - b.tarifaReferencialBase : b.calificacionPromedio - a.calificacionPromedio)
      if (!lista.length) return <EmptyState title="No encontramos prestadores" text="Prueba ampliando el radio de búsqueda o cambia tus filtros." action="Volver a buscar" onClick={() => setScreen("search")} />
      return <div className="provider-list">{lista.map((p: any) => <ProviderCard key={p.id} prestador={p} onClick={() => { seleccionar({ prestadorId: p.id }); setScreen("provider") }} />)}</div>
    }}</Consulta>
  </AppContent>
}

function ProviderProfile({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { sesion, seleccion, seleccionar } = useSesion()
  const ficha = useDatos(() => api.bffPrestador(seleccion.prestadorId!), [seleccion.prestadorId])
  const [contactando, setContactando] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // "Contactar" no es solo navegar: abre el Contacto en Mercado y la
  // Conversación en Comunicación, y deja el chat listo con ese hilo activo.
  const contactar = async () => {
    const demandanteId = sesion?.perfilDemandante?.id
    if (!demandanteId || !seleccion.prestadorId) return
    setContactando(true); setError(null)
    try {
      const { conversacion } = await api.contactar(demandanteId, seleccion.prestadorId)
      seleccionar({ conversacionId: conversacion.id })
      setScreen("chat")
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "No pudimos abrir el chat. Intenta de nuevo.")
      setContactando(false)
    }
  }

  if (!seleccion.prestadorId) return <AppContent><PageHeader title="Perfil del prestador" onBack={() => setScreen("results")} /><EmptyState title="Elige un prestador" text="Vuelve a los resultados y selecciona a alguien para ver su perfil." action="Ver resultados" onClick={() => setScreen("results")} /></AppContent>

  return <AppContent>
    <PageHeader title="Perfil del prestador" onBack={() => setScreen("results")} />
    <Consulta estado={ficha} filas={3}>{datos => {
      const p = datos.perfil
      const pila = p.nombreCompleto.split(" ")[0]
      return <>
        <div className="profile-hero">
          <Avatar nombre={p.nombreCompleto} id={p.id} large />
          <h2>{p.nombreCompleto}</h2>
          <p>{datos.oficios?.items[0]?.oficio.nombre ?? p.descripcion}</p>
          {p.insigniaVerificado && <Verified />}
          <Rating value={p.calificacionPromedio} count={p.totalResenas} />
          <span className="distance"><MapPin /> {p.ubicacionPrincipal.barrio}, {p.ubicacionPrincipal.municipio}</span>
        </div>
        <div className="profile-stats">
          <div><strong>{datos.oficios?.items[0]?.anosExperiencia ?? 0} años</strong><span>Experiencia</span></div>
          <div><strong>{datos.contrataciones?.totalCompletadas ?? 0}</strong><span>Contrataciones</span></div>
          <div><strong>{pesos(p.tarifaReferencialBase)}</strong><span>Tarifa desde</span></div>
        </div>
        <section className="detail-section"><h2>Sobre {pila}</h2><p>{p.descripcion}</p></section>
        <section className="detail-section">
          <div className="section-heading"><h2>Oficios ofrecidos</h2>{p.planActual === "PRO" && <Badge tone="brand">Plan Pro</Badge>}</div>
          <div className="chips">{(datos.oficios?.items ?? []).map(o => <span key={o.oficio.id}>{o.oficio.nombre} · {pesos(o.tarifaReferencial)}</span>)}</div>
        </section>
        {(datos.ultimasResenas?.items.length ?? 0) > 0 && <section className="detail-section">
          <div className="section-heading"><h2>Reseñas recientes</h2></div>
          <div className="review-list">{(datos.ultimasResenas?.items ?? []).map((r: Resena) => (
            <div className="review" key={r.id}><div className="row-between"><Rating value={r.puntuacion} /><span className="review-date">{fecha(r.fecha)}</span></div><p>{r.comentario}</p></div>
          ))}</div>
        </section>}
        {error && <div className="form-error" role="alert"><CircleAlert /> {error}</div>}
        <div className="sticky-action">
          <Button onClick={contactar} disabled={contactando}>
            {contactando ? <><Loader2 className="spin" /> Abriendo chat…</> : <>Contactar a {pila} <MessageCircle data-icon="inline-end" /></>}
          </Button>
        </div>
      </>
    }}</Consulta>
  </AppContent>
}

/**
 * Confirmación de tarifa dentro del chat.
 *
 * El servicio no nace de la conversación: nace de que las dos partes acepten
 * un valor. Mientras solo haya aceptado una, la otra ve el botón de aceptar;
 * cuando aceptan ambas se crea la contratación y, con ella, la comisión queda
 * congelada al plan que el prestador tenga en ese instante.
 */
function PanelTarifa({ hilo, cerrada, onCambio, setScreen }: { hilo: ConversacionEnBandeja; cerrada: boolean; onCambio: () => void; setScreen: (s: Screen) => void }) {
  const { sesion, seleccionar } = useSesion()
  const esDemandante = sesion?.rol === "Demandante"
  const rol = esDemandante ? "DEMANDANTE" : "PRESTADOR"
  const [proponiendo, setProponiendo] = useState(false)
  const [valor, setValor] = useState("")
  const [medioPago, setMedioPago] = useState("EFECTIVO")
  const [enviando, setEnviando] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const prestadorId = hilo.contacto.prestadorId
  const comision = useDatos(() => api.comisionVigente(prestadorId), [prestadorId])
  const acuerdo = hilo.acuerdo
  const yaAcepte = acuerdo ? (esDemandante ? acuerdo.aceptadoDemandante : acuerdo.aceptadoPrestador) : false

  const ejecutar = async (accion: () => Promise<unknown>) => {
    setEnviando(true); setError(null)
    try {
      await accion()
      setProponiendo(false)
      setValor("")
      onCambio()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "No pudimos registrar la tarifa.")
    } finally {
      setEnviando(false)
    }
  }

  const proponer = () => ejecutar(() => api.proponerTarifa({
    contactoId: hilo.contacto.id,
    conversacionId: hilo.id,
    demandanteId: hilo.contacto.demandanteId,
    prestadorId,
    valorPropuesto: Number(valor),
    medioPago,
    propuestoPor: rol,
  }))

  // Chat cerrado: el servicio ya terminó y se calificó. Solo queda el resumen.
  if (cerrada) {
    const c = hilo.contratacion
    return <div className="tarifa-panel confirmado">
      <div className="row-between">
        <div><span className="eyebrow">SERVICIO CERRADO</span><strong>{c ? pesos(c.valorAcordado) : "Sin servicio"}</strong></div>
        <Badge tone="neutral"><LockKeyhole /> Solo lectura</Badge>
      </div>
      <p className="fine-print">
        {c ? `${enTitulo(c.medioPago)} · comisión ${pesos(c.montoComision)}` : "Este chat terminó sin acordar un servicio"}
        {hilo.fechaCierre && <> · cerrado el {fecha(hilo.fechaCierre)}</>}
      </p>
      {c && <Button variant="secondary" onClick={() => { seleccionar({ contratacionId: c.id }); setScreen(esDemandante ? "tracking" : "checkin") }}>
        Ver el servicio <ArrowRight data-icon="inline-end" />
      </Button>}
    </div>
  }

  // El servicio ya existe: el panel deja de negociar y pasa a ser el acceso a él.
  // Cuando el demandante lo califica queda cerrado, y con él este chat.
  if (acuerdo?.estado === "ACEPTADO" && hilo.contratacion) {
    const c = hilo.contratacion
    const terminado = hilo.ciclo?.terminada ?? false
    const abrir = (pantalla: Screen) => { seleccionar({ contratacionId: c.id }); setScreen(pantalla) }
    return <div className="tarifa-panel confirmado">
      <div className="row-between">
        <div><span className="eyebrow">{terminado ? "SERVICIO TERMINADO" : "SERVICIO CONFIRMADO"}</span><strong>{pesos(c.valorAcordado)}</strong></div>
        <Badge tone={tonoEstado(c.estado)}>{enTitulo(c.estado)}</Badge>
      </div>
      <p className="fine-print">
        {terminado
          ? esDemandante
            ? "Califica el servicio para cerrarlo. Este chat quedará cerrado; para contratar de nuevo abrirás uno nuevo."
            : "El servicio se cierra cuando tu cliente lo califique, y con él este chat."
          : <>{enTitulo(c.medioPago)} · comisión {pesos(c.montoComision)} ({Math.round(c.porcentajeComisionAplicado * 100)}%)
              {acuerdo.planPrestadorAlAcordar && <> · congelada con el plan {acuerdo.planPrestadorAlAcordar} del día del acuerdo</>}</>}
      </p>
      {terminado && esDemandante && hilo.ciclo?.puedeCalificar.DEMANDANTE
        ? <Button onClick={() => abrir("rating")}>Calificar y cerrar el servicio <Star data-icon="inline-end" /></Button>
        : <Button variant="secondary" onClick={() => abrir(esDemandante ? "tracking" : "checkin")}>
            {esDemandante ? "Ver el estado del servicio" : terminado ? "Ver el servicio" : "Gestionar check-in / check-out"} <ArrowRight data-icon="inline-end" />
          </Button>}
    </div>
  }

  // Hay una propuesta abierta: quien no la ha aceptado decide.
  if (acuerdo?.estado === "PENDIENTE" && !proponiendo) {
    return <div className="tarifa-panel">
      <div className="row-between">
        <div><span className="eyebrow">TARIFA PROPUESTA</span><strong>{pesos(acuerdo.valorPropuesto)}</strong></div>
        <Badge tone="warning">{yaAcepte ? "Esperando a la otra parte" : "Requiere tu confirmación"}</Badge>
      </div>
      <p className="fine-print">
        {enTitulo(acuerdo.medioPago)} · propuesta por {acuerdo.propuestoPor === "DEMANDANTE" ? "el demandante" : "el prestador"}
        {comision.datos && <> · al confirmar, la comisión queda en {Math.round(comision.datos.porcentajeComision * 100)}% (plan {comision.datos.plan})</>}
      </p>
      {error && <div className="form-error" role="alert"><CircleAlert /> {error}</div>}
      {yaAcepte
        ? <Button variant="secondary" onClick={() => setProponiendo(true)} disabled={enviando}>Proponer otro valor</Button>
        : <div className="tarifa-acciones">
            <Button onClick={() => ejecutar(() => api.aceptarTarifa(acuerdo.id, rol))} disabled={enviando}>
              {enviando ? <><Loader2 className="spin" /> Confirmando…</> : <><Check data-icon="inline-start" /> Confirmar tarifa</>}
            </Button>
            <Button variant="secondary" onClick={() => setProponiendo(true)} disabled={enviando}>Proponer otro valor</Button>
          </div>}
    </div>
  }

  if (!proponiendo) return <div className="tarifa-panel">
    <div><span className="eyebrow">ANTES DE EMPEZAR</span><strong>Acuerden la tarifa del servicio</strong></div>
    <p className="fine-print">El servicio se crea cuando ambas partes confirman el mismo valor. La comisión se fija con el plan del prestador en ese momento y no cambia después.</p>
    <Button onClick={() => setProponiendo(true)}><CreditCard data-icon="inline-start" /> Confirmar tarifa</Button>
  </div>

  return <div className="tarifa-panel">
    <div><span className="eyebrow">PROPONER TARIFA</span><strong>¿Cuánto cuesta este servicio?</strong></div>
    <div className="form-grid">
      <label>Valor acordado<div className="input-icon"><span>$</span>
        <input type="number" min="1" value={valor} autoFocus onChange={e => { setValor(e.target.value); setError(null) }} placeholder="100000" />
      </div></label>
      <label>Medio de pago<select value={medioPago} onChange={e => setMedioPago(e.target.value)}>
        <option value="EFECTIVO">Efectivo</option>
        <option value="PLATAFORMA">Plataforma</option>
      </select></label>
    </div>
    {comision.datos && <p className="fine-print">
      Comisión que se aplicaría: {Math.round(comision.datos.porcentajeComision * 100)}% (plan {comision.datos.plan})
      {Number(valor) > 0 && <> · {pesos(Number(valor) * comision.datos.porcentajeComision)}</>}
    </p>}
    {error && <div className="form-error" role="alert"><CircleAlert /> {error}</div>}
    <div className="tarifa-acciones">
      <Button onClick={proponer} disabled={enviando || !(Number(valor) > 0)}>
        {enviando ? <><Loader2 className="spin" /> Enviando…</> : <>Enviar propuesta <ArrowRight data-icon="inline-end" /></>}
      </Button>
      <Button variant="secondary" onClick={() => { setProponiendo(false); setError(null) }} disabled={enviando}>Cancelar</Button>
    </div>
  </div>
}

function Chat({ role, setScreen }: { role: Role; setScreen: (s: Screen) => void }) {
  const { sesion, seleccion, seleccionar } = useSesion()
  const perfilId = perfilIdDe(sesion)
  const esDemandante = sesion?.rol === "Demandante"
  // La bandeja (qué chats hay, su acuerdo y su estado) se refresca por sondeo;
  // los mensajes del chat abierto llegan por WebSocket.
  const bandeja = useDatos(
    () => api.bandeja(esDemandante ? { demandanteId: perfilId } : { prestadorId: perfilId }),
    [perfilId, esDemandante],
    { cadaMs: EN_VIVO_MS },
  )

  const hilos = bandeja.datos?.items ?? []
  const abiertos = hilos.filter(h => h.estado !== "CERRADA")
  const cerrados = hilos.filter(h => h.estado === "CERRADA")
  const activa = hilos.find(h => h.id === seleccion.conversacionId) ?? abiertos[0] ?? hilos[0]
  const chat = useChatEnVivo(activa?.id, sesion?.usuario.id, { alCerrarse: bandeja.recargar })
  const cerrada = activa?.estado === "CERRADA" || chat.cerrada

  // Acordar una tarifa antes de haber hablado no tiene sentido: la propuesta
  // aparece cuando las dos partes ya escribieron. Un acuerdo que ya existe se
  // sigue mostrando: para haber nacido, ya conversaron.
  const remitentes = new Set(chat.mensajes.map(m => m.remitenteId))
  const conversacionIniciada = remitentes.size >= 2 || Boolean(activa?.acuerdo) || cerrada

  const [texto, setTexto] = useState("")
  const [enviando, setEnviando] = useState(false)
  const [abriendo, setAbriendo] = useState(false)
  const final = useRef<HTMLDivElement>(null)

  // Cada mensaje nuevo (propio o de la otra parte) lleva la vista al final.
  useEffect(() => { final.current?.scrollIntoView({ block: "end" }) }, [chat.mensajes.length, chat.otroEscribiendo])

  const enviar = async () => {
    const contenido = texto.trim()
    if (!contenido || !activa || cerrada) return
    setEnviando(true)
    try {
      await chat.enviar(contenido)
      setTexto("")
    } catch {
      // El hook ya dejó el error a la vista.
    } finally {
      setEnviando(false)
    }
  }

  // Solo el demandante contacta: al hacerlo sobre un chat cerrado se abre otro.
  const iniciarNuevo = async () => {
    if (!activa) return
    setAbriendo(true)
    try {
      const { conversacion } = await api.contactar(activa.contacto.demandanteId, activa.contacto.prestadorId)
      seleccionar({ conversacionId: conversacion.id })
      bandeja.recargar()
    } finally {
      setAbriendo(false)
    }
  }

  const estadoConexion = cerrada ? "Chat cerrado"
    : chat.conexion === "conectado" ? (chat.enLinea > 1 ? "En línea" : "Desconectado")
    : chat.conexion === "reconectando" ? "Reconectando…" : chat.conexion === "sin-conexion" ? "Sin conexión" : "Conectando…"

  return <AppContent>
    <PageHeader title="Mensajes" subtitle={activa ? `Con ${activa.otraParteNombre}` : "Tus conversaciones"}
      onBack={() => setScreen(role === "Demandante" ? "search" : "dashboard")} />
    <Consulta estado={bandeja}>{() => {
      if (!hilos.length) return <EmptyState
        title="Aún no tienes conversaciones"
        text={esDemandante
          ? "Busca un prestador y pulsa «Contactar» para abrir el chat."
          : "Cuando alguien te contacte, su mensaje aparecerá aquí."}
        action={esDemandante ? "Buscar prestadores" : undefined}
        onClick={esDemandante ? () => setScreen("search") : undefined} />

      return <>
        {abiertos.length > 1 && <div className="conversation-tabs">{abiertos.map(h => (
          <button key={h.id} className={h.id === activa?.id ? "active" : ""}
            onClick={() => seleccionar({ conversacionId: h.id })}>
            {h.otraParteNombre}
          </button>
        ))}</div>}
        {cerrados.length > 0 && <div className="conversation-tabs anteriores">
          <span>Chats anteriores</span>
          {abiertos.length === 1 && <button className={abiertos[0].id === activa?.id ? "active" : ""}
            onClick={() => seleccionar({ conversacionId: abiertos[0].id })}>Actual · {abiertos[0].otraParteNombre}</button>}
          {cerrados.map(h => (
            <button key={h.id} className={h.id === activa?.id ? "active" : ""}
              onClick={() => seleccionar({ conversacionId: h.id })}>
              {h.otraParteNombre} · {fecha(h.fechaCierre ?? h.fechaInicio)}
            </button>
          ))}
        </div>}

        {activa && <div className="chat-identity">
          <Avatar nombre={activa.otraParteNombre} id={activa.otraParteId} />
          <div><strong>{activa.otraParteNombre}</strong>
            <span className={`chat-estado ${!cerrada && chat.conexion === "conectado" && chat.enLinea > 1 ? "en-linea" : ""}`}>{estadoConexion}</span>
          </div>
        </div>}

        {activa && (conversacionIniciada
          ? <PanelTarifa hilo={activa} cerrada={cerrada} onCambio={bandeja.recargar} setScreen={setScreen} />
          : <p className="chat-nota">Cuando ambos hayan escrito podrán acordar la tarifa del servicio.</p>)}

        <div className="chat-body">
          {chat.cargando && !chat.mensajes.length
            ? <div className="provider-list"><SkeletonCard /></div>
            : chat.mensajes.length
              ? chat.mensajes.map((m: Mensaje) => (
                  <div key={m.id} className={`message ${m.remitenteId === sesion!.usuario.id ? "mine" : "theirs"}`}>
                    <p>{m.contenido}</p><span>{hora(m.fechaEnvio)}</span>
                  </div>
                ))
              : <p className="chat-vacio">Todavía no hay mensajes. Escribe el primero.</p>}
          {chat.otroEscribiendo && !cerrada && <p className="chat-escribiendo">{activa?.otraParteNombre} está escribiendo…</p>}
          <div ref={final} />
        </div>

        {chat.error && <div className="form-error" role="alert"><CircleAlert /> {chat.error}</div>}
        {cerrada
          ? <div className="chat-cerrado">
              <LockKeyhole />
              <p><strong>Este chat se cerró al terminar el servicio.</strong><br />
                {esDemandante
                  ? `Para volver a contratar a ${activa?.otraParteNombre}, inicia un chat nuevo.`
                  : "Si el cliente te vuelve a contratar, abrirá un chat nuevo."}</p>
              {esDemandante && <Button onClick={iniciarNuevo} disabled={abriendo}>
                {abriendo ? <><Loader2 className="spin" /> Abriendo…</> : <>Iniciar un chat nuevo <MessageCircle data-icon="inline-end" /></>}
              </Button>}
            </div>
          : <div className="chat-compose">
              <input value={texto} placeholder="Escribe un mensaje..."
                onChange={e => { setTexto(e.target.value); chat.avisarEscribiendo() }}
                onKeyDown={e => { if (e.key === "Enter" && !e.nativeEvent.isComposing) enviar() }} />
              <Button onClick={enviar} disabled={enviando || !texto.trim()}>
                {enviando ? <Loader2 className="spin" /> : <Send />}
              </Button>
            </div>}

        <div className="chat-cta"><Button variant="secondary" onClick={() => setScreen(role === "Demandante" ? "tracking" : "requests")}>
          {role === "Demandante" ? "Ver mis contrataciones" : "Ver solicitudes"} <ArrowRight data-icon="inline-end" />
        </Button></div>
      </>
    }}</Consulta>
  </AppContent>
}

function ContratacionCard({ contratacion, onClick }: { contratacion: Contratacion; onClick: () => void }) {
  return <button className="job-card as-button" onClick={onClick}>
    <div className="row-between">
      <div><span className="eyebrow">{fecha(contratacion.fechaEjecucion ?? contratacion.fechaSolicitud)}</span><h3>{pesos(contratacion.valorAcordado)}</h3></div>
      <Badge tone={tonoEstado(contratacion.estado)}>{enTitulo(contratacion.estado)}</Badge>
    </div>
    <p><CreditCard /> {enTitulo(contratacion.medioPago)} · Comisión {pesos(contratacion.montoComision)}</p>
    <span className="distance">Ver detalle <ChevronRight /></span>
  </button>
}

function Tracking({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { sesion, seleccion, seleccionar } = useSesion()
  const perfilId = perfilIdDe(sesion)
  const lista = useDatos(() => api.contrataciones({ demandanteId: perfilId }), [perfilId], { cadaMs: EN_VIVO_MS })
  const detalle = useDatos(() => api.bffContratacion(seleccion.contratacionId!), [seleccion.contratacionId], { cadaMs: EN_VIVO_MS })

  if (seleccion.contratacionId) return <AppContent>
    <PageHeader title="Seguimiento" subtitle={`Contratación ${seleccion.contratacionId.slice(-4)}`} onBack={() => seleccionar({ contratacionId: undefined })} />
    <Consulta estado={detalle} filas={3}>{datos => <>
      <div className="tracking-card">
        <div className="tracking-head">
          <div><span className="eyebrow">{datos.oficio?.nombre ?? "SERVICIO"}</span><h2>{datos.prestador?.nombreCompleto ?? "Prestador"}</h2></div>
          <span className="tracking-price">{pesos(datos.contratacion.valorAcordado)}</span>
        </div>
        <div className="timeline">{(datos.timeline?.pasos ?? []).map((paso, i) => (
          <div className={`timeline-item ${paso.alcanzado ? "done" : ""}`} key={paso.estado}>
            <div className="timeline-dot">{paso.alcanzado ? <Check /> : <span>{i + 1}</span>}</div>
            <div><strong>{enTitulo(paso.estado)}</strong><span>{paso.alcanzado ? "Completado" : "Pendiente"}</span></div>
          </div>
        ))}</div>
        {datos.timeline?.duracionMinutos != null && <p className="fine-print">Duración registrada entre check-in y check-out: {datos.timeline.duracionMinutos} minutos.</p>}
      </div>
      {datos.prestador && <div className="provider-mini">
        <Avatar nombre={datos.prestador.nombreCompleto} id={datos.prestador.id} />
        <div><strong>{datos.prestador.nombreCompleto}</strong><span>{datos.prestador.insigniaVerificado ? <><ShieldCheck /> Prestador verificado</> : "Sin verificar"}</span></div>
        <button className="icon-button" onClick={() => setScreen("chat")}><MessageCircle /></button>
      </div>}
      {(datos.incidentes?.total ?? 0) > 0 && <div className="alert-box"><CircleAlert /><p><strong>{datos.incidentes!.total} incidente(s) reportado(s).</strong><br />{datos.incidentes!.items[0].evidenciaDescripcion}</p></div>}
      {(datos.resenas?.total ?? 0) > 0 && <section className="detail-section"><div className="section-heading"><h2>Reseñas de esta contratación</h2></div><div className="review-list">{datos.resenas!.items.map(r => <div className="review" key={r.id}><div className="row-between"><Rating value={r.puntuacion} /><span className="review-date">{fecha(r.fecha)}</span></div><p>{r.comentario}</p></div>)}</div></section>}
      {datos.ciclo.cerrada && <div className="alert-box info"><Check /><p>
        <strong>Servicio cerrado.</strong><br />
        {datos.contratacion.estado === "CANCELADA"
          ? "Este servicio se canceló."
          : "Ya lo calificaste, así que no admite más calificaciones ni reportes."}
        {" "}Para volver a contratar a {datos.prestador?.nombreCompleto ?? "este prestador"} se abre un chat nuevo.
      </p></div>}
      {datos.ciclo.cerrada && <Button onClick={async () => {
        // Contactar sobre un servicio cerrado abre un chat nuevo para el siguiente.
        const { conversacion } = await api.contactar(datos.contratacion.demandanteId, datos.contratacion.prestadorId)
        seleccionar({ conversacionId: conversacion.id })
        setScreen("chat")
      }}>Contratar de nuevo <MessageCircle data-icon="inline-end" /></Button>}
      {datos.ciclo.puedeCalificar.DEMANDANTE && <Button onClick={() => setScreen("rating")}>Calificar y cerrar el servicio <ArrowRight data-icon="inline-end" /></Button>}
      {datos.ciclo.puedeReportarIncidente && <Button variant="danger" onClick={() => setScreen("incident")}>Reportar incidente</Button>}
    </>}</Consulta>
  </AppContent>

  return <AppContent>
    <PageHeader title="Mis contrataciones" subtitle={lista.datos ? `${lista.datos.total} en total` : "Cargando…"} />
    <Consulta estado={lista} filas={3}>{datos => datos.items.length
      ? <div className="request-list">{datos.items.map(c => <ContratacionCard key={c.id} contratacion={c} onClick={() => seleccionar({ contratacionId: c.id })} />)}</div>
      : <EmptyState title="Aún no tienes contrataciones" text="Cuando contrates a un prestador, harás seguimiento desde aquí." action="Buscar un oficio" onClick={() => setScreen("search")} />}
    </Consulta>
  </AppContent>
}

function RatingScreen({ role, setScreen }: { role: Role; setScreen: (s: Screen) => void }) {
  const { sesion, seleccion } = useSesion()
  const esPrestador = role === "Prestador"
  const [puntuacion, setPuntuacion] = useState(0)
  const [comentario, setComentario] = useState("")
  const [enviando, setEnviando] = useState(false)
  const [enviada, setEnviada] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const detalle = useDatos(() => api.bffContratacion(seleccion.contratacionId!), [seleccion.contratacionId])

  // Quien escribe es el perfil de quien entró; quien recibe, la otra parte de
  // esta misma contratación. La reseña cuenta de inmediato para el promedio
  // publicado del receptor.
  const autorId = perfilIdDe(sesion)
  const receptorId = esPrestador
    ? detalle.datos?.contratacion.demandanteId
    : detalle.datos?.contratacion.prestadorId

  const publicar = async () => {
    if (!seleccion.contratacionId || !autorId || !receptorId) return
    setEnviando(true); setError(null)
    try {
      await api.publicarResena({
        contratacionId: seleccion.contratacionId, autorId, receptorId,
        puntuacion, comentario: comentario.trim(),
      })
      setEnviada(true)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "No pudimos publicar la reseña.")
    } finally {
      setEnviando(false)
    }
  }

  if (!seleccion.contratacionId) return <AppContent>
    <PageHeader title="Califica tu experiencia" onBack={() => setScreen(esPrestador ? "dashboard" : "tracking")} />
    <EmptyState title="Elige un servicio" text="Abre una contratación completada para calificarla."
      action="Ver contrataciones" onClick={() => setScreen(esPrestador ? "requests" : "tracking")} />
  </AppContent>

  const volver = () => setScreen(esPrestador ? "checkin" : "tracking")
  const rol = esPrestador ? "PRESTADOR" : "DEMANDANTE"
  const ciclo = detalle.datos?.ciclo

  // Se entra aquí desde botones que ya consultan el ciclo, pero la pantalla no
  // lo da por hecho: el servicio pudo cerrarse en otra pestaña o dispositivo.
  if (ciclo && !enviada && !ciclo.puedeCalificar[rol]) return <AppContent>
    <PageHeader title="Califica tu experiencia" onBack={volver} />
    <EmptyState
      title={ciclo.calificadaPor[rol] ? "Ya calificaste este servicio" : "Aún no puedes calificar"}
      text={ciclo.calificadaPor[rol]
        ? "Cada servicio se califica una sola vez. Para contratar de nuevo, inicia un chat nuevo."
        : "La calificación se habilita cuando el servicio termina (después del check-out)."}
      action="Volver" onClick={volver} />
  </AppContent>

  return <AppContent>
    <PageHeader title="Califica tu experiencia" subtitle="Contratación completada" onBack={volver} />
    {enviada
      ? <div className="success-state"><Check /><h2>Reseña publicada</h2>
          <p>{esPrestador
            ? "Gracias por calificar a tu cliente."
            : "Ya cuenta para la reputación del prestador. El servicio y su chat quedaron cerrados: para volver a contratarlo se abre un chat nuevo."}</p>
          <Button onClick={volver}>Volver <ArrowRight data-icon="inline-end" /></Button>
        </div>
      : <>
          <div className="rating-card">
            <h2>{esPrestador ? "¿Cómo fue trabajar con tu cliente?" : "¿Cómo fue tu servicio?"}</h2>
            <p>Tu opinión ayuda a otras personas a contratar con confianza.</p>
            <div className="stars-input">{[1, 2, 3, 4, 5].map(i => (
              <button key={i} aria-label={`${i} estrellas`} onClick={() => setPuntuacion(i)}>
                <Star fill={i <= puntuacion ? "currentColor" : "none"} />
              </button>
            ))}</div>
            <textarea value={comentario} onChange={e => setComentario(e.target.value)}
              placeholder="Cuéntanos más sobre tu experiencia (opcional)" />
          </div>
          {error && <div className="form-error" role="alert"><CircleAlert /> {error}</div>}
          <Button disabled={!puntuacion || enviando || !receptorId} onClick={publicar}>
            {enviando ? <><Loader2 className="spin" /> Publicando…</> : <>Publicar reseña <Check data-icon="inline-end" /></>}
          </Button>
        </>}
  </AppContent>
}

function Incident({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { seleccion } = useSesion()
  const detalle = useDatos(() => api.bffContratacion(seleccion.contratacionId!), [seleccion.contratacionId])
  const [tipo, setTipo] = useState("TRABAJO_INCOMPLETO")
  const [descripcion, setDescripcion] = useState("")
  const [enviando, setEnviando] = useState(false)
  const [enviado, setEnviado] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reportar = async () => {
    if (!seleccion.contratacionId || !descripcion.trim()) return
    setEnviando(true); setError(null)
    try {
      await api.reportarIncidente(seleccion.contratacionId, tipo, descripcion.trim())
      setEnviado(true)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "No pudimos registrar el reporte.")
    } finally {
      setEnviando(false)
    }
  }

  const ciclo = detalle.datos?.ciclo
  if (ciclo && !enviado && !ciclo.puedeReportarIncidente) return <AppContent>
    <PageHeader title="Reportar incidente" onBack={() => setScreen("tracking")} />
    <EmptyState
      title={ciclo.incidenteReportado ? "Ya reportaste un incidente" : "Este servicio ya está cerrado"}
      text={ciclo.incidenteReportado
        ? "El caso sigue abierto en Soporte; puedes ver su estado en el seguimiento de la contratación."
        : "Los incidentes se reportan durante el servicio o justo al terminarlo, antes de calificarlo."}
      action="Volver al seguimiento" onClick={() => setScreen("tracking")} />
  </AppContent>

  return <AppContent>
    <PageHeader title="Reportar incidente" onBack={() => setScreen("tracking")} />
    <div className="alert-box"><CircleAlert /><p><strong>Tu seguridad es prioridad.</strong><br />Nuestro equipo revisará el caso y te contactará.</p></div>
    {enviado
      ? <div className="success-state"><Check /><h2>Reporte registrado</h2><p>Queda abierto en el contexto de Soporte y aparecerá en el seguimiento de la contratación.</p><Button onClick={() => setScreen("tracking")}>Volver al seguimiento <ArrowRight data-icon="inline-end" /></Button></div>
      : <>
        <div className="form-stack">
          <label>Tipo de incidente<select value={tipo} onChange={e => setTipo(e.target.value)}>
            <option value="TRABAJO_INCOMPLETO">Trabajo incompleto</option>
            <option value="COBRO_INDEBIDO">Cobro indebido</option>
            <option value="RETRASO">Retraso</option>
            <option value="DANO_MATERIAL">Daño material</option>
          </select></label>
          <label>Descripción<textarea value={descripcion} onChange={e => setDescripcion(e.target.value)} placeholder="Cuéntanos qué sucedió..." /></label>
        </div>
        {error && <div className="form-error" role="alert"><CircleAlert /> {error}</div>}
        <Button variant="danger" onClick={reportar} disabled={enviando || !descripcion.trim()}>
          {enviando ? <><Loader2 className="spin" /> Enviando…</> : <>Enviar reporte <ArrowRight data-icon="inline-end" /></>}
        </Button>
      </>}
  </AppContent>
}

function Notifications({ role, setScreen }: { role: Role; setScreen: (s: Screen) => void }) {
  const { sesion } = useSesion()
  const lista = useDatos(() => api.notificaciones(sesion!.usuario.id), [sesion?.usuario.id], { cadaMs: EN_VIVO_MS })
  return <AppContent>
    <PageHeader title="Notificaciones" subtitle="Mantente al día" onBack={() => setScreen(role === "Demandante" ? "search" : "dashboard")} />
    <Consulta estado={lista} filas={3}>{datos => datos.items.length
      ? <div className="notification-list">{datos.items.map(n => (
          <div className={`notification-item ${n.leida ? "" : "unread"}`} key={n.id}>
            <div className={`notification-icon ${n.tipo.includes("APROBADA") || n.tipo.includes("ACEPTADA") ? "success" : "info"}`}><Bell /></div>
            <div><strong>{enTitulo(n.tipo)}</strong><p>{n.contenido}</p><span>{fecha(n.fechaEnvio)} · {n.canal}</span></div>
            {!n.leida && <span className="notification-dot static" />}
          </div>
        ))}</div>
      : <EmptyState title="No tienes notificaciones" text="Te avisaremos cuando haya novedades sobre tus contrataciones." />}
    </Consulta>
  </AppContent>
}

function Profile({ role, setScreen }: { role: Role; setScreen: (s: Screen) => void }) {
  const { sesion, salir } = useSesion()
  if (!sesion) return null
  const u = sesion.usuario
  const ubicacion = sesion.perfilDemandante?.ubicacionPrincipal ?? sesion.perfilPrestador?.ubicacionPrincipal
  return <AppContent>
    <PageHeader title="Mi perfil" subtitle="Gestiona tu información" onBack={() => setScreen(role === "Demandante" ? "search" : "dashboard")} />
    <div className="profile-summary">
      <Avatar nombre={u.nombreCompleto} id={u.id} large />
      <div><h2>{u.nombreCompleto}</h2><p>{u.correo}</p><Badge tone="success"><Check /> Cuenta verificada</Badge></div>
    </div>
    <div className="settings-list">
      <div className="setting-static"><MapPin /><span><strong>Ubicación principal</strong><small>{ubicacion ? `${ubicacion.barrio}, ${ubicacion.municipio}` : "Sin definir"}</small></span></div>
      <div className="setting-static"><UserRound /><span><strong>Documento</strong><small>{u.tipoDocumento} {u.numeroDocumento}</small></span></div>
      <div className="setting-static"><MessageCircle /><span><strong>Teléfono</strong><small>{u.telefono}</small></span></div>
      <div className="setting-static"><CalendarDays /><span><strong>Miembro desde</strong><small>{fecha(u.fechaRegistro)}</small></span></div>
    </div>
    <Button variant="secondary" onClick={salir}><LogOut data-icon="inline-start" /> Cerrar sesión</Button>
  </AppContent>
}

// ---------------------------------------------------------------------------
// Prestador
// ---------------------------------------------------------------------------

function Dashboard({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { sesion, seleccionar } = useSesion()
  const prestadorId = sesion?.perfilPrestador?.id
  const ficha = useDatos(() => api.bffPrestador(prestadorId!), [prestadorId], { cadaMs: EN_VIVO_MS })
  const contrataciones = useDatos(() => api.contrataciones({ prestadorId, size: 5 }), [prestadorId], { cadaMs: EN_VIVO_MS })
  const { solicitudes } = useSolicitudes()
  const porResponder = solicitudes.filter(s => s.grupo === "responder").length
  const pila = sesion?.usuario.nombreCompleto.split(" ")[0] ?? ""

  return <AppContent>
    <PageHeader title={`Hola, ${pila}`} subtitle={new Date().toLocaleDateString("es-CO", { weekday: "long", day: "numeric", month: "long" })}
      action={<button className="icon-button" onClick={() => setScreen("notifications")} aria-label="Notificaciones"><Bell /></button>} />
    <Consulta estado={ficha} filas={3}>{datos => <>
      <div className="dashboard-hero">
        <div><span className="eyebrow">ESTADO DE TU CUENTA</span><h2>Estás listo para trabajar</h2><p><span className="online-dot" /> Disponible para nuevas solicitudes</p></div>
        {datos.perfil.insigniaVerificado && <Verified />}
      </div>
      {porResponder > 0 && <button className="alert-box solicitudes-aviso" onClick={() => setScreen("requests")}>
        <Bell /><p><strong>{porResponder === 1 ? "Tienes 1 solicitud" : `Tienes ${porResponder} solicitudes`} esperando tu respuesta.</strong><br />Toca para verlas.</p><ChevronRight />
      </button>}
      <div className="metric-grid">
        <div><span>Contrataciones</span><strong>{datos.contrataciones?.total ?? 0}</strong><small>{datos.contrataciones?.totalCompletadas ?? 0} completadas</small></div>
        <div><span>Calificación</span><strong>{datos.perfil.calificacionPromedio.toFixed(1)} <Star fill="currentColor" /></strong><small>{datos.perfil.totalResenas} reseñas</small></div>
      </div>
      <section>
        <div className="section-heading"><h2>Tus contrataciones</h2><button className="text-button" onClick={() => setScreen("requests")}>Ver todas</button></div>
        <Consulta estado={contrataciones} filas={2}>{lista => lista.items.length
          ? <div className="request-list">{lista.items.slice(0, 2).map(c => <ContratacionCard key={c.id} contratacion={c} onClick={() => { seleccionar({ contratacionId: c.id }); setScreen("checkin") }} />)}</div>
          : <EmptyState title="Sin contrataciones todavía" text="Cuando alguien te contrate, aparecerá aquí." />}
        </Consulta>
      </section>
      <section>
        <div className="section-heading"><h2>Resumen de billetera</h2><button className="text-button" onClick={() => setScreen("wallet")}>Ver billetera</button></div>
        <div className="wallet-mini"><WalletCards /><div><span>Comisiones acumuladas</span><strong>{pesos(datos.finanzas?.comisionesAcumuladas ?? 0)}</strong></div><ChevronRight /></div>
      </section>
    </>}</Consulta>
  </AppContent>
}

type Solicitud = {
  hilo: ConversacionEnBandeja
  grupo: "responder" | "curso"
  titulo: string
  detalle: string
  tono: "warning" | "brand" | "success"
  etiqueta: string
  accion: "chat" | "checkin"
  fecha: string
}

/**
 * Traduce un chat de la bandeja a lo que el prestador tiene que hacer con él.
 * Una solicitud es cualquier cosa que viene del cliente antes o durante el
 * servicio: que abra el chat, que escriba, que proponga una tarifa. Por eso se
 * arma desde la bandeja y no desde las contrataciones, que solo existen cuando
 * ya hubo acuerdo.
 */
function clasificarSolicitud(hilo: ConversacionEnBandeja, miUsuarioId: string, vuelve: boolean): Solicitud | null {
  const { acuerdo, contratacion, ciclo, ultimoMensaje } = hilo
  // Un chat cerrado es historia: su servicio ya terminó y se calificó.
  if (hilo.estado === "CERRADA") return null
  const base = { hilo, fecha: acuerdo?.fechaPropuesta ?? ultimoMensaje?.fechaEnvio ?? hilo.fechaInicio }

  if (acuerdo?.estado === "PENDIENTE") {
    return acuerdo.aceptadoPrestador
      ? { ...base, grupo: "curso", titulo: `Propusiste ${pesos(acuerdo.valorPropuesto)}`, detalle: "Esperando a que el cliente confirme la tarifa.", tono: "brand", etiqueta: "Esperando al cliente", accion: "chat" }
      : { ...base, grupo: "responder", titulo: `Te propone ${pesos(acuerdo.valorPropuesto)}`, detalle: `${enTitulo(acuerdo.medioPago)} · confírmala o propón otro valor en el chat.`, tono: "warning", etiqueta: "Confirma la tarifa", accion: "chat" }
  }
  if (acuerdo?.estado === "ACEPTADO" && contratacion) {
    if (ciclo?.terminada) return { ...base, grupo: "curso", titulo: `Servicio terminado · ${pesos(contratacion.valorAcordado)}`, detalle: "Se cierra cuando el cliente lo califique.", tono: "success", etiqueta: "Por calificar", accion: "checkin" }
    return { ...base, grupo: contratacion.checkIn ? "curso" : "responder", titulo: `Servicio confirmado · ${pesos(contratacion.valorAcordado)}`, detalle: contratacion.checkIn ? "En curso: haz check-out al terminar." : "Haz check-in cuando llegues donde el cliente.", tono: contratacion.checkIn ? "brand" : "warning", etiqueta: enTitulo(contratacion.estado), accion: "checkin" }
  }
  // Sin acuerdo todavía: un chat que empieza. Es solicitud si el cliente lo
  // abrió o escribió y aún no se le ha respondido.
  if (!ultimoMensaje) return { ...base, grupo: "responder", titulo: vuelve ? "Vuelve a contratarte" : "Quiere contratarte", detalle: "Abrió un chat contigo. Salúdalo para empezar.", tono: "warning", etiqueta: vuelve ? "Cliente que vuelve" : "Nuevo contacto", accion: "chat" }
  if (ultimoMensaje.remitenteId !== miUsuarioId) return {
    ...base, grupo: "responder",
    titulo: vuelve ? "Vuelve a escribirte" : "Te escribió",
    detalle: `«${ultimoMensaje.contenido.slice(0, 90)}»`,
    tono: "warning", etiqueta: vuelve ? "Cliente que vuelve" : "Sin responder", accion: "chat",
  }
  return null
}

function SolicitudCard({ solicitud, onAbrir }: { solicitud: Solicitud; onAbrir: () => void }) {
  const { hilo } = solicitud
  return <button className="job-card as-button solicitud-card" onClick={onAbrir}>
    <div className="row-between">
      <div className="solicitud-quien"><Avatar nombre={hilo.otraParteNombre} id={hilo.otraParteId} /><div><strong>{hilo.otraParteNombre}</strong><span>{fecha(solicitud.fecha)} · {hora(solicitud.fecha)}</span></div></div>
      <Badge tone={solicitud.tono}>{solicitud.etiqueta}</Badge>
    </div>
    <h3>{solicitud.titulo}</h3>
    <p>{solicitud.detalle}</p>
    <span className="distance">{solicitud.accion === "chat" ? "Abrir el chat" : "Gestionar el servicio"} <ChevronRight /></span>
  </button>
}

/** Solicitudes del prestador a partir de su bandeja, en vivo. */
function useSolicitudes() {
  const { sesion } = useSesion()
  const prestadorId = sesion?.perfilPrestador?.id
  const bandeja = useDatos(() => api.bandeja({ prestadorId }), [prestadorId], { cadaMs: EN_VIVO_MS })
  const hilos = bandeja.datos?.items ?? []
  // Un cliente "vuelve" si ya tuvo con este prestador un chat que se cerró.
  const conChatCerrado = new Set(hilos.filter(h => h.estado === "CERRADA").map(h => h.contacto.id))
  const solicitudes = hilos
    .map(h => clasificarSolicitud(h, sesion?.usuario.id ?? "", conChatCerrado.has(h.contacto.id)))
    .filter((s): s is Solicitud => s !== null)
    .sort((a, b) => b.fecha.localeCompare(a.fecha))
  return { bandeja, solicitudes }
}

function Requests({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { sesion, seleccionar } = useSesion()
  const prestadorId = sesion?.perfilPrestador?.id
  const { bandeja, solicitudes } = useSolicitudes()
  const historial = useDatos(() => api.contrataciones({ prestadorId }), [prestadorId], { cadaMs: EN_VIVO_MS })
  const responder = solicitudes.filter(s => s.grupo === "responder")
  const enCurso = solicitudes.filter(s => s.grupo === "curso")

  const abrir = (s: Solicitud) => {
    seleccionar({ conversacionId: s.hilo.id, contratacionId: s.hilo.contratacion?.id })
    setScreen(s.accion)
  }

  return <AppContent>
    <PageHeader title="Solicitudes" subtitle="Clientes que te buscan y tus servicios" onBack={() => setScreen("dashboard")}
      action={<Badge tone="success">En vivo</Badge>} />
    <Consulta estado={bandeja} filas={2}>{() => <>
      <section>
        <div className="section-heading"><h2>Necesitan tu respuesta</h2><span className="fine-print">{responder.length}</span></div>
        {responder.length
          ? <div className="request-list">{responder.map(s => <SolicitudCard key={s.hilo.id} solicitud={s} onAbrir={() => abrir(s)} />)}</div>
          : <p className="fine-print">Nada pendiente. Cuando un cliente te escriba o te proponga una tarifa, aparecerá aquí al instante.</p>}
      </section>
      {enCurso.length > 0 && <section>
        <div className="section-heading"><h2>En curso</h2><span className="fine-print">{enCurso.length}</span></div>
        <div className="request-list">{enCurso.map(s => <SolicitudCard key={s.hilo.id} solicitud={s} onAbrir={() => abrir(s)} />)}</div>
      </section>}
    </>}</Consulta>
    <section>
      <div className="section-heading"><h2>Historial de servicios</h2></div>
      <Consulta estado={historial} filas={2}>{datos => datos.items.length
        ? <div className="request-list">{datos.items.map(c => <ContratacionCard key={c.id} contratacion={c} onClick={() => { seleccionar({ contratacionId: c.id }); setScreen("checkin") }} />)}</div>
        : <p className="fine-print">Aún no has cerrado ningún servicio.</p>}
      </Consulta>
    </section>
  </AppContent>
}

const ORDEN_ETAPAS: EtapaCobro[] = ["SIN_EVENTO", "EN_OUTBOX", "PUBLICADO", "COBRADO"]
const ms = (valor: number | null | undefined) =>
  valor == null ? "—" : valor < 1000 ? `${Math.round(valor)} ms` : `${(valor / 1000).toFixed(1)} s`

/**
 * El cobro de la comisión ya no ocurre dentro del check-out: viaja como evento
 * CONTRATACION_COMPLETADA (outbox de Contrataciones → SNS → cola SQS → worker de
 * Monetización). Esta tarjeta sondea el avance hasta que la comisión aparece en
 * la billetera, para que el desacoplamiento se vea en vivo.
 */
function SeguimientoCobro({ contratacionId, setScreen }: { contratacionId: string; setScreen: (s: Screen) => void }) {
  const [sondeando, setSondeando] = useState(true)
  const estado = useDatos(() => api.estadoCobro(contratacionId), [contratacionId], { cadaMs: sondeando ? 1000 : null })
  const etapa = estado.datos?.etapa
  useEffect(() => { if (etapa === "COBRADO") setSondeando(false) }, [etapa])

  if (!estado.datos) return null
  const { evento, cobro } = estado.datos
  const alcanzada = (e: EtapaCobro) => ORDEN_ETAPAS.indexOf(estado.datos!.etapa) >= ORDEN_ETAPAS.indexOf(e)
  const reintentando = evento?.estado === "PENDIENTE" && evento.intentos > 0

  const pasos = [
    {
      etapa: "EN_OUTBOX" as const,
      titulo: "Evento guardado con el cierre",
      detalle: evento ? `Outbox de Contrataciones · ${hora(evento.fechaCreacion)}` : "Esperando el check-out",
    },
    {
      etapa: "PUBLICADO" as const,
      titulo: "Publicado en SNS → cola SQS",
      detalle: evento?.fechaPublicacion
        ? `contratacion-completada-topic · ${hora(evento.fechaPublicacion)}`
        : reintentando ? `Broker no disponible, reintento ${evento!.intentos}…` : "El relay lo publicará en instantes",
    },
    {
      etapa: "COBRADO" as const,
      titulo: "Comisión cargada a tu billetera",
      detalle: cobro?.movimiento
        ? `Worker de Monetización · ${pesos(Math.abs(cobro.movimiento.monto))}${cobro.evento?.latenciaMs != null ? ` · ${ms(cobro.evento.latenciaMs)} desde el check-out` : ""}`
        : "Esperando a que Monetización consuma el evento",
    },
  ]

  return <div className="tracking-card">
    <div className="tracking-head">
      <div><span className="eyebrow">COBRO POR EVENTOS</span><h2>Comisión del servicio</h2></div>
      {etapa === "COBRADO" ? <Badge tone="success">Cobrada</Badge> : <Badge tone="warning"><Loader2 className="spin" /> En curso</Badge>}
    </div>
    <div className="timeline">{pasos.map((paso, i) => (
      <div className={`timeline-item ${alcanzada(paso.etapa) ? "done" : ""}`} key={paso.etapa}>
        <div className="timeline-dot">{alcanzada(paso.etapa) ? <Check /> : <span>{i + 1}</span>}</div>
        <div><strong>{paso.titulo}</strong><span>{paso.detalle}</span></div>
      </div>
    ))}</div>
    {etapa === "COBRADO" && cobro?.billetera && <p className="fine-print">
      Saldo pendiente con JOBBI: {pesos(cobro.billetera.saldoPendiente)}. El pago fue en efectivo: lo cobraste tú y nos liquidas la comisión.
    </p>}
    {etapa === "COBRADO" && <Button variant="secondary" onClick={() => setScreen("wallet")}>Ver mi billetera <WalletCards data-icon="inline-end" /></Button>}
  </div>
}

function Checkin({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { seleccion } = useSesion()
  const detalle = useDatos(() => api.bffContratacion(seleccion.contratacionId!), [seleccion.contratacionId], { cadaMs: EN_VIVO_MS })
  const [marcando, setMarcando] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cobro, setCobro] = useState<{ monto: number; saldo: number } | null>(null)

  // El prestador no ve la línea de etapas del demandante: solo los dos botones
  // que le tocan. Marcar la salida es además lo que dispara el cobro, porque
  // con pago en efectivo no hay ningún otro momento en que la plataforma cobre;
  // el cobro en sí llega después, por el evento CONTRATACION_COMPLETADA.
  const marcar = async (accion: "in" | "out") => {
    if (!seleccion.contratacionId) return
    setMarcando(true); setError(null)
    try {
      if (accion === "in") {
        await api.checkIn(seleccion.contratacionId)
      } else {
        const { cobro: resultado } = await api.checkOut(seleccion.contratacionId)
        if (resultado) setCobro({ monto: Math.abs(resultado.movimiento.monto), saldo: resultado.billetera.saldoPendiente })
      }
      detalle.recargar()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "No pudimos registrar la marca.")
    } finally {
      setMarcando(false)
    }
  }

  if (!seleccion.contratacionId) return <AppContent><PageHeader title="Contratación activa" onBack={() => setScreen("dashboard")} /><EmptyState title="Elige una contratación" text="Abre una desde tus solicitudes para gestionar el check-in." action="Ver solicitudes" onClick={() => setScreen("requests")} /></AppContent>

  return <AppContent>
    <PageHeader title="Contratación activa" subtitle={`#${seleccion.contratacionId.slice(-4)}`} onBack={() => setScreen("requests")} />
    <Consulta estado={detalle} filas={3}>{datos => {
      const c = datos.contratacion
      return <>
        <div className="job-detail">
          <div className="job-detail-top">
            {datos.demandante && <Avatar nombre={datos.demandante.nombreCompleto} id={datos.demandante.id} />}
            <div><h2>{datos.demandante?.nombreCompleto ?? "Cliente"}</h2><p><MapPin /> {datos.demandante?.ubicacionPrincipal.barrio}, {datos.demandante?.ubicacionPrincipal.municipio}</p></div>
            <Badge tone={tonoEstado(c.estado)}>{enTitulo(c.estado)}</Badge>
          </div>
          <div className="time-block"><CalendarDays /><div><span>{fecha(c.fechaEjecucion ?? c.fechaSolicitud)}</span><strong>{datos.oficio?.nombre ?? "Servicio"} · {pesos(c.valorAcordado)}</strong></div></div>
          <div className="checkin-status">
            <div className={c.checkIn ? "check-step done" : "check-step current"}><div>{c.checkIn ? <Check /> : "1"}</div><span>Check-in{c.checkIn && ` · ${hora(c.checkIn)}`}</span></div>
            <div className="check-line" />
            <div className={c.checkOut ? "check-step done" : "check-step"}><div>{c.checkOut ? <Check /> : "2"}</div><span>Check-out{c.checkOut && ` · ${hora(c.checkOut)}`}</span></div>
          </div>
        </div>
        <div className="summary-box"><span>Comisión aplicada</span><strong>{pesos(c.montoComision)} ({Math.round(c.porcentajeComisionAplicado * 100)}%)</strong><span>Medio de pago</span><strong>{enTitulo(c.medioPago)}</strong></div>
        <p className="fine-print">Este porcentaje se fijó al cerrar el acuerdo y ya no cambia, aunque cambies de plan durante el servicio.</p>

        {error && <div className="form-error" role="alert"><CircleAlert /> {error}</div>}
        {cobro && <div className="alert-box info"><WalletCards /><p><strong>Comisión cargada a tu billetera: {pesos(cobro.monto)}.</strong><br />El pago fue en efectivo, así que lo cobras tú y nos liquidas la comisión. Saldo pendiente: {pesos(cobro.saldo)}.</p></div>}
        {/* Con Pub/Sub el check-out no trae el cobro: se sigue el evento en vivo. */}
        {c.checkOut && !cobro && c.medioPago === "EFECTIVO" && <SeguimientoCobro contratacionId={c.id} setScreen={setScreen} />}

        {!c.checkIn && <Button onClick={() => marcar("in")} disabled={marcando}>
          {marcando ? <><Loader2 className="spin" /> Registrando…</> : <>Hacer check-in <Check data-icon="inline-end" /></>}
        </Button>}
        {c.checkIn && !c.checkOut && <Button onClick={() => marcar("out")} disabled={marcando}>
          {marcando ? <><Loader2 className="spin" /> Cerrando servicio…</> : <>Hacer check-out <Check data-icon="inline-end" /></>}
        </Button>}
        {c.checkOut && <div className="success-state"><Check /><h2>{datos.ciclo.cerrada ? "Servicio cerrado" : "Servicio completado"}</h2>
          <p>{datos.ciclo.cerrada
            ? "Tu cliente ya lo calificó y el chat quedó cerrado. Si te vuelve a contratar, abrirá uno nuevo."
            : "Tu cliente ya puede calificarlo; al hacerlo, el servicio queda cerrado."}</p>
          {datos.ciclo.puedeCalificar.PRESTADOR
            ? <Button variant="secondary" onClick={() => setScreen("rating")}>Calificar al cliente <Star data-icon="inline-end" /></Button>
            : datos.ciclo.calificadaPor.PRESTADOR && <p className="fine-print">Ya calificaste a este cliente.</p>}
        </div>}

        <Button variant="secondary" onClick={() => setScreen("chat")}>Contactar al cliente <MessageCircle data-icon="inline-end" /></Button>
      </>
    }}</Consulta>
  </AppContent>
}

function Verification({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { sesion } = useSesion()
  const prestadorId = sesion?.perfilPrestador?.id
  const estado = useDatos(() => api.estadoVerificacion(prestadorId!), [prestadorId])
  return <AppContent>
    <PageHeader title="Verificación de identidad" subtitle="Tu confianza es nuestra prioridad" onBack={() => setScreen("profile")} />
    <div className="verification-panel pending">
      <div className="verification-icon"><ShieldCheck /></div>
      <Badge tone="success">Aprobada</Badge>
      <h2>Tu identidad está verificada</h2>
      <p>En esta fase la validación con Truora se da por superada, así que tu cuenta queda activa desde el registro y la insignia aparece de inmediato.</p>
      <Consulta estado={estado} filas={1}>{datos => (
        <div className="verification-benefits">
          <span><Check /> {datos.aprobadas} de {datos.totalVerificaciones} verificaciones aprobadas</span>
          {datos.tiposAprobados.map(tipo => <span key={tipo}><ShieldCheck /> {enTitulo(tipo)}</span>)}
        </div>
      )}</Consulta>
    </div>
  </AppContent>
}

function Plans({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { sesion, actualizarPrestador } = useSesion()
  const prestadorId = sesion?.perfilPrestador?.id
  const actual = sesion?.perfilPrestador?.planActual ?? "FREE"
  const planes = useDatos(() => api.planes(), ["planes"])
  const [cambiando, setCambiando] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // El cambio de plan es inmediato y reversible: es la palanca con la que se
  // demuestra en vivo qué comisión se congela en cada acuerdo.
  const cambiar = async (plan: "FREE" | "PRO") => {
    if (!prestadorId || plan === actual) return
    setCambiando(plan); setError(null)
    try {
      const { perfil } = await api.cambiarPlan(prestadorId, plan)
      actualizarPrestador(perfil)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "No pudimos cambiar el plan.")
    } finally {
      setCambiando(null)
    }
  }

  const porcentaje = (plan: string) =>
    planes.datos?.items.find(p => p.plan === plan)?.porcentajeComision

  const tarjeta = (plan: "FREE" | "PRO", titulo: string, ventajas: string[]) => {
    const esActual = actual === plan
    const pct = porcentaje(plan)
    const mensual = planes.datos?.items.find(p => p.plan === plan)?.valorMensual ?? 0
    return <div className={`plan-card ${plan === "PRO" ? "pro " : ""}${esActual ? "current" : ""}`}>
      <div className="row-between"><h3>{titulo}</h3>{esActual && <Badge tone="success">Actual</Badge>}</div>
      <strong>{pct !== undefined ? `${Math.round(pct * 100)}%` : "—"} <small>comisión</small></strong>
      {mensual > 0 && <span className="monthly">{pesos(mensual)} COP / mes</span>}
      <ul>{ventajas.map(v => <li key={v}><Check /> {v}</li>)}</ul>
      <Button variant={esActual ? "secondary" : "primary"} disabled={esActual || cambiando !== null} onClick={() => cambiar(plan)}>
        {cambiando === plan ? <><Loader2 className="spin" /> Cambiando…</> : esActual ? "Tu plan actual" : `Cambiar a ${titulo}`}
      </Button>
    </div>
  }

  return <AppContent>
    <PageHeader title="Planes para Prestadores" subtitle="Crece con JOBBI" onBack={() => setScreen("profile")} />
    <div className="plans-intro"><Sparkles /><h2>Gana más en cada contratación</h2><p>Tu plan vive en Identidad y su tarifa en Monetización. Cambiarlo aquí escribe en los dos.</p></div>
    {error && <div className="form-error" role="alert"><CircleAlert /> {error}</div>}
    <Consulta estado={planes} filas={2}>{() => <div className="plan-grid">
      {tarjeta("FREE", "Free", ["Perfil público", "Recibe solicitudes", "Reseñas verificadas"])}
      {tarjeta("PRO", "Pro", ["Todo lo de Free", "Mayor visibilidad", "Soporte prioritario"])}
    </div>}</Consulta>
    <div className="alert-box info"><LockKeyhole /><p>
      <strong>El cambio aplica hacia adelante.</strong><br />
      La comisión de cada servicio se congela cuando ambas partes confirman la tarifa: los acuerdos ya cerrados conservan el porcentaje de ese día aunque cambies de plan durante el servicio.
    </p></div>
  </AppContent>
}

function Wallet({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { sesion } = useSesion()
  const prestadorId = sesion?.perfilPrestador?.id
  const resumen = useDatos(() => api.resumenFinanciero(prestadorId!), [prestadorId], { cadaMs: EN_VIVO_MS })
  const billeteraId = resumen.datos?.billetera?.id
  const movimientos = useDatos(() => api.movimientos(billeteraId!), [billeteraId], { cadaMs: EN_VIVO_MS })

  return <AppContent>
    <PageHeader title="Billetera" subtitle="Gestiona tus ingresos" onBack={() => setScreen("dashboard")} />
    <Consulta estado={resumen} filas={2}>{datos => {
      const b = datos.billetera
      return <>
        <div className={`wallet-balance ${b?.bloqueada ? "blocked" : ""}`}>
          <span>Saldo pendiente</span>
          <strong>{pesos(b?.saldoPendiente ?? 0)} <small>COP</small></strong>
          <p>{b?.bloqueada ? <><LockKeyhole /> Billetera bloqueada por saldo superior al umbral</> : <><Clock3 /> Se liquida cada viernes</>}</p>
        </div>
        {datos.suscripcionActiva && <div className="alert-box info"><Sparkles /><p><strong>Suscripción Pro activa.</strong><br />Renueva el {fecha(datos.suscripcionActiva.fechaRenovacion)} por {pesos(datos.suscripcionActiva.valorMensual)}.</p></div>}
        <section>
          <div className="section-heading"><h2>Movimientos</h2><span className="fine-print">{datos.totalMovimientos} en total</span></div>
          <Consulta estado={movimientos} filas={2}>{lista => lista.items.length
            ? <div className="movement-list">{lista.items.map(m => (
                <div className="movement" key={m.id}>
                  <div className="movement-icon"><WalletCards /></div>
                  <div><strong>{enTitulo(m.tipo)}</strong><span>{fecha(m.fecha)}</span></div>
                  <b className={m.monto > 0 ? "positive" : ""}>{m.monto > 0 ? "+" : "−"}{pesos(Math.abs(m.monto))}</b>
                </div>
              ))}</div>
            : <EmptyState title="Sin movimientos" text="Tus comisiones y abonos aparecerán aquí." />}
          </Consulta>
        </section>
      </>
    }}</Consulta>
  </AppContent>
}

function Reviews({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { sesion } = useSesion()
  const prestadorId = sesion?.perfilPrestador?.id
  const resumen = useDatos(() => api.resumenResenas(prestadorId!), [prestadorId])
  const lista = useDatos(() => api.resenas({ receptorId: prestadorId }), [prestadorId])
  return <AppContent>
    <PageHeader title="Reseñas recibidas" subtitle="Tu reputación en JOBBI" onBack={() => setScreen("profile")} />
    <Consulta estado={resumen} filas={1}>{datos => (
      <div className="review-summary"><strong>{datos.promedio.toFixed(1)}</strong><div><Rating value={datos.promedio} /><span>{datos.total} reseñas aprobadas</span></div></div>
    )}</Consulta>
    <Consulta estado={lista} filas={3}>{datos => datos.items.length
      ? <div className="review-list">{datos.items.map(r => (
          <div className="review" key={r.id}>
            <div className="row-between"><Rating value={r.puntuacion} /><Badge tone={r.estadoModeracion === "APROBADA" ? "success" : "warning"}>{enTitulo(r.estadoModeracion)}</Badge></div>
            <p>{r.comentario}</p><span>{fecha(r.fecha)}</span>
          </div>
        ))}</div>
      : <EmptyState title="Aún no tienes reseñas" text="Completa tu primera contratación para empezar a construir tu reputación." action="Ver solicitudes" onClick={() => setScreen("requests")} />}
    </Consulta>
  </AppContent>
}

function PrestadorProfile({ setScreen }: { setScreen: (s: Screen) => void }) {
  const { sesion, salir } = useSesion()
  const p = sesion?.perfilPrestador
  if (!sesion || !p) return null
  return <AppContent>
    <PageHeader title="Mi perfil de Prestador" subtitle="Así te ven tus clientes" onBack={() => setScreen("dashboard")} />
    <div className="profile-summary">
      <Avatar nombre={sesion.usuario.nombreCompleto} id={p.id} large />
      <div><h2>{sesion.usuario.nombreCompleto}</h2><p>{p.descripcion}</p>{p.insigniaVerificado && <Verified />}</div>
    </div>
    <div className="settings-list">
      <button onClick={() => setScreen("verification")}><ShieldCheck /><span><strong>Verificación de identidad</strong><small>Estado: {enTitulo(p.estadoVerificacionActual)}</small></span><ChevronRight /></button>
      <button onClick={() => setScreen("plans")}><Sparkles /><span><strong>Plan actual</strong><small>{p.planActual} · cámbialo para ver cómo varía tu comisión</small></span><ChevronRight /></button>
      <button onClick={() => setScreen("reviews")}><Star /><span><strong>Reseñas recibidas</strong><small>{p.calificacionPromedio.toFixed(1)} · {p.totalResenas} reseñas</small></span><ChevronRight /></button>
      <button onClick={() => setScreen("wallet")}><WalletCards /><span><strong>Billetera</strong><small>Comisiones y movimientos</small></span><ChevronRight /></button>
    </div>
    <Button variant="secondary" onClick={salir}><LogOut data-icon="inline-start" /> Cerrar sesión</Button>
  </AppContent>
}

// ---------------------------------------------------------------------------
// Administración
// ---------------------------------------------------------------------------

/**
 * Tablero del Pub/Sub: productor (outbox + relay), broker (cola y DLQ) y
 * consumidor (worker SQS). Se refresca cada 2 s, así que durante una prueba de
 * carga se ve la cola llenarse y drenarse.
 */
function MonitorPubSub() {
  const estado = useDatos(() => api.estadoPubSub(), ["pubsub"], { cadaMs: 2000 })
  const cola = (c: Cola | undefined) => c?.error ? "—" : `${c?.visibles ?? 0}`

  return <section className="admin-list-card pubsub-monitor">
    <div className="section-heading">
      <div><span className="eyebrow">PUB/SUB · CONTRATACION_COMPLETADA</span><h2>Cobro de comisiones por eventos</h2></div>
      {estado.datos && (estado.datos.modo === "EVENTOS"
        ? <Badge tone="success">En vivo</Badge>
        : <Badge tone="warning">Modo síncrono</Badge>)}
    </div>
    <Consulta estado={estado} filas={1}>{({ productor: p, consumidor: c }) => {
      const colas = c?.colas
      const dlq = colas?.dlq.visibles ?? 0
      const latencia = (l: Latencias | undefined) => l?.muestras ? `${ms(l.p95)} p95 · ${ms(l.promedio)} prom.` : "sin datos"
      return <>
        <div className="admin-metric-grid">
          <div className="admin-metric"><span>Outbox pendiente</span><strong>{p?.pendientes ?? "—"}</strong><small>{p?.publicados ?? 0} publicados en SNS</small></div>
          <div className="admin-metric"><span>Cola SQS</span><strong>{cola(colas?.principal)}</strong><small>{colas?.principal.enVuelo ?? 0} en proceso</small></div>
          <div className="admin-metric"><span>DLQ</span><strong className={dlq > 0 ? "danger-text" : ""}>{cola(colas?.dlq)}</strong><small>tras 5 intentos fallidos</small></div>
          <div className="admin-metric"><span>Eventos procesados</span><strong>{c?.cobrosProcesados ?? "—"}</strong><small>{c?.desdeArranque.duplicadosDescartados ?? 0} duplicados descartados</small></div>
        </div>
        <div className="status-row"><span className={`status-dot ${p?.relay.activo && !p.relay.ultimoError ? "success" : "danger"}`} /><span>Relay outbox → SNS</span><strong>{!p ? "sin respuesta" : !p.relay.activo ? "apagado" : p.relay.ultimoError ? "reintentando" : "activo"}</strong></div>
        <div className="status-row"><span className={`status-dot ${c?.conectado ? "success" : "danger"}`} /><span>Worker SQS de Monetización</span><strong>{!c ? "sin respuesta" : !c.workerActivo ? "apagado" : c.conectado ? `conectado · ${c.hilos} hilos` : "esperando la cola"}</strong></div>
        <div className="status-row"><span className="status-dot success" /><span>Latencia outbox → SNS</span><strong>{latencia(p?.latenciaPublicacionMs)}</strong></div>
        <div className="status-row"><span className="status-dot success" /><span>Latencia check-out → cobro</span><strong>{latencia(c?.latenciaExtremoAExtremoMs)}</strong></div>
        {Object.entries(c?.porResultado ?? {}).map(([resultado, n]) => (
          <div className="status-row" key={resultado}><span className="status-dot warning" /><span>{enTitulo(resultado)}</span><strong>{n}</strong></div>
        ))}
        {(p?.relay.ultimoError || c?.ultimoError) && <p className="fine-print">Último error: {p?.relay.ultimoError ?? c?.ultimoError}</p>}
      </>
    }}</Consulta>
  </section>
}

function AdminDashboard({ setScreen }: { setScreen: (s: Screen) => void }) {
  const metricas = useDatos(() => api.bffMetricasAdmin(), ["admin"])
  return <AppContent>
    <PageHeader title="Panel de administración" subtitle="Datos reales de los nueve contextos" action={<Badge tone="success">En vivo</Badge>} />
    <Consulta estado={metricas} filas={4}>{datos => {
      const porEstado = Object.entries(datos.contrataciones?.porEstado ?? {})
      const maximo = Math.max(1, ...porEstado.map(([, n]) => n))
      return <>
        <div className="admin-metric-grid">
          <div className="admin-metric"><span>Valor completado</span><strong>{pesos(datos.contrataciones?.valorTotalCompletado ?? 0)}</strong><small>{datos.contrataciones?.total ?? 0} contrataciones</small></div>
          <div className="admin-metric"><span>Comisión generada</span><strong>{pesos(datos.contrataciones?.comisionTotalCompletada ?? 0)}</strong><small>sobre servicios completados</small></div>
          <div className="admin-metric"><span>Prestadores</span><strong>{datos.totalPrestadores}</strong><small>activos en la plataforma</small></div>
          <div className="admin-metric"><span>Incidentes abiertos</span><strong>{datos.incidentes?.abiertos ?? 0}</strong><small>de {datos.incidentes?.total ?? 0} reportados</small></div>
        </div>
        <section className="admin-chart-card">
          <div className="section-heading"><div><span className="eyebrow">CONTRATACIONES</span><h2>Distribución por estado</h2></div></div>
          <div className="bar-chart" aria-label="Contrataciones por estado">
            {porEstado.map(([estado, n]) => (
              <div className="bar-column" key={estado}>
                <div className="bar-track"><i style={{ height: `${(n / maximo) * 100}%` }} /></div>
                <span>{enTitulo(estado)}</span>
              </div>
            ))}
          </div>
        </section>
        <div className="admin-columns">
          <section className="admin-list-card">
            <div className="section-heading"><h2>Catálogo por categoría</h2></div>
            {(datos.catalogo?.items ?? []).map((fila, i) => (
              <div className="ranking-row" key={fila.categoria.id}>
                <span className="rank">0{i + 1}</span>
                <div><strong>{fila.categoria.nombre}</strong><div className="rank-bar"><i style={{ width: `${Math.min(100, fila.totalOfertas * 30)}%` }} /></div></div>
                <b>{fila.totalOfertas}</b>
              </div>
            ))}
          </section>
          <section className="admin-list-card">
            <div className="section-heading"><h2>Canal de adquisición</h2></div>
            {(datos.canalAdquisicion?.items ?? []).map(fila => (
              <div className="status-row" key={fila.aliado.nombre}>
                <span className={`status-dot ${fila.tasaActivacion >= 1 ? "success" : fila.tasaActivacion > 0 ? "warning" : "danger"}`} />
                <span>{fila.aliado.nombre}</span>
                <strong>{fila.activados}/{fila.totalReferidos}</strong>
              </div>
            ))}
            <div className="status-row"><span className="status-dot warning" /><span>Reseñas en moderación</span><strong>{datos.resenasEnModeracion}</strong></div>
            <Button variant="secondary" onClick={() => setScreen("notifications")}>Revisar alertas <ArrowRight data-icon="inline-end" /></Button>
          </section>
        </div>
      </>
    }}</Consulta>
    <MonitorPubSub />
  </AppContent>
}

// ---------------------------------------------------------------------------
// Raíz
// ---------------------------------------------------------------------------

const PANTALLAS_PUBLICAS: Screen[] = ["map", "landing", "role", "register", "login", "coverage"]

export default function JobbiApp() {
  const [sesion, setSesion] = useState<Sesion | null>(null)
  const [role, setRole] = useState<Role>("Demandante")
  const [screen, setScreen] = useState<Screen>("map")
  const [seleccion, setSeleccion] = useState<Seleccion>({})
  const [listo, setListo] = useState(false)

  // Rehidrata la sesión del navegador antes de pintar, para no mostrar el
  // onboarding un instante a quien ya había entrado.
  useEffect(() => {
    const guardada = leerSesionGuardada()
    if (guardada) {
      setSesion(guardada)
      setRole(guardada.rol)
      setScreen(guardada.rol === "Prestador" ? "dashboard" : "search")
    }
    setListo(true)
  }, [])

  const entrar = (nueva: Sesion) => {
    guardarSesion(nueva)
    setSesion(nueva)
    setRole(nueva.rol)
    setSeleccion({})
    // Es aquí donde el rol decide la pantalla: demandante busca, prestador gestiona.
    setScreen(nueva.rol === "Prestador" ? "dashboard" : "search")
  }

  const salir = () => {
    guardarSesion(null)
    setSesion(null)
    setSeleccion({})
    setRole("Demandante")
    setScreen("landing")
  }

  // El perfil de prestador vive en la sesión guardada, así que cambiar de plan
  // tiene que reescribirla: si no, el navegador seguiría mostrando el anterior
  // hasta el siguiente login.
  const actualizarPrestador = (perfil: PerfilPrestador) => setSesion(previa => {
    if (!previa) return previa
    const nueva = { ...previa, perfilPrestador: perfil }
    guardarSesion(nueva)
    return nueva
  })

  const contexto = useMemo(
    () => ({
      sesion,
      seleccion,
      seleccionar: (cambios: Seleccion) => setSeleccion(previo => ({ ...previo, ...cambios })),
      actualizarPrestador,
      salir,
    }),
    [sesion, seleccion],
  )

  if (!listo) return <div className="jobbi-app onboarding" />

  // Sin sesión solo se puede estar en el onboarding; con sesión, nunca en él.
  const pantalla: Screen = sesion
    ? (PANTALLAS_PUBLICAS.includes(screen) ? (role === "Prestador" ? "dashboard" : "search") : screen)
    : (PANTALLAS_PUBLICAS.includes(screen) ? screen : "landing")

  const contenido = (() => {
    switch (pantalla) {
      case "map": return <MapScreen setScreen={setScreen} />
      case "landing": return <Landing setScreen={setScreen} />
      case "role": return <RoleScreen setScreen={setScreen} setRole={setRole} />
      case "register": return <Register role={role} setScreen={setScreen} entrar={entrar} />
      case "login": return <Login setScreen={setScreen} entrar={entrar} />
      case "coverage": return <Coverage role={role} setScreen={setScreen} />
      case "search": return <SearchScreen setScreen={setScreen} />
      case "results": return <Results setScreen={setScreen} />
      case "provider": return <ProviderProfile setScreen={setScreen} />
      case "chat": return <Chat role={role} setScreen={setScreen} />
      case "tracking": return <Tracking setScreen={setScreen} />
      case "rating": return <RatingScreen role={role} setScreen={setScreen} />
      case "incident": return <Incident setScreen={setScreen} />
      case "notifications": return <Notifications role={role} setScreen={setScreen} />
      case "profile": return role === "Prestador" ? <PrestadorProfile setScreen={setScreen} /> : <Profile role={role} setScreen={setScreen} />
      case "dashboard": return <Dashboard setScreen={setScreen} />
      case "admin": return <AdminDashboard setScreen={setScreen} />
      case "requests": return <Requests setScreen={setScreen} />
      case "checkin": return <Checkin setScreen={setScreen} />
      case "verification": return <Verification setScreen={setScreen} />
      case "plans": return <Plans setScreen={setScreen} />
      case "wallet": return <Wallet setScreen={setScreen} />
      case "reviews": return <Reviews setScreen={setScreen} />
      default: return <SearchScreen setScreen={setScreen} />
    }
  })()

  return <SesionContext.Provider value={contexto}>
    {PANTALLAS_PUBLICAS.includes(pantalla)
      ? <div className="jobbi-app onboarding">{contenido}</div>
      : <div className="jobbi-app"><AppShell role={role} setRole={setRole} screen={pantalla} setScreen={setScreen}>{contenido}</AppShell></div>}
  </SesionContext.Provider>
}
