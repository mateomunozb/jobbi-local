export type Role = "Demandante" | "Prestador" | "Admin"
export type Screen =
  | "map"
  | "landing"
  | "role"
  | "register"
  | "otp"
  | "coverage"
  | "search"
  | "results"
  | "provider"
  | "chat"
  | "request"
  | "tracking"
  | "payment"
  | "rating"
  | "incident"
  | "notifications"
  | "profile"
  | "location"
  | "personal"
  | "help"
  | "dashboard"
  | "admin"
  | "requests"
  | "checkin"
  | "verification"
  | "plans"
  | "wallet"
  | "withdraw"
  | "reviews"

export type Provider = {
  id: string
  name: string
  oficio: string
  categoria: string
  rating: number
  reviews: number
  rate: string
  verified: boolean
  experience: number
  initials: string
  accent: string
}

export const categories = ["Todos", "Hogar", "Belleza", "Bienestar", "Jardinería"]
export const providers: Provider[] = [
  { id: "p1", name: "Carlos Ramírez", oficio: "Plomería y reparaciones", categoria: "Hogar", rating: 4.9, reviews: 42, rate: "$85.000 / visita", verified: true, experience: 8, initials: "CR", accent: "blue" },
  { id: "p2", name: "Valentina Gómez", oficio: "Manicure y pedicure", categoria: "Belleza", rating: 4.8, reviews: 28, rate: "$55.000 / servicio", verified: true, experience: 5, initials: "VG", accent: "rose" },
  { id: "p3", name: "Andrés Molina", oficio: "Jardinería", categoria: "Jardinería", rating: 4.6, reviews: 19, rate: "$60.000 / visita", verified: false, experience: 6, initials: "AM", accent: "green" },
]

export const statuses = ["Solicitada", "Aceptada", "En curso", "Check-in", "Check-out", "Completada"]
export const navDemandante = [
  ["search", "Buscar"], ["chat", "Mensajes"], ["tracking", "Contrataciones"], ["notifications", "Notificaciones"], ["profile", "Perfil"],
] as const
export const navPrestador = [
  ["dashboard", "Dashboard"], ["requests", "Solicitudes"], ["chat", "Mensajes"], ["wallet", "Billetera"], ["profile", "Perfil"],
] as const
export const navAdmin = [
  ["admin", "Métricas"], ["requests", "Solicitudes"], ["notifications", "Alertas"], ["profile", "Perfil"],
] as const

export const messages = [
  { from: "provider", text: "Hola, claro que sí. Puedo ayudarte con la reparación.", time: "10:32" },
  { from: "me", text: "¡Perfecto! ¿Tienes disponibilidad mañana en la tarde?", time: "10:34" },
  { from: "provider", text: "Tengo un espacio a las 2:00 p. m. Te confirmo por aquí.", time: "10:35" },
]

export const notifications = [
  { title: "Solicitud aceptada", body: "Carlos aceptó tu solicitud de plomería.", time: "Hace 12 min", type: "success" },
  { title: "Nueva reseña", body: "Tu contratación fue completada. Déjanos tu opinión.", time: "Ayer", type: "info" },
  { title: "Verificación aprobada", body: "Tu identidad ya está verificada.", time: "12 sep", type: "success" },
]

export const walletMovements = [
  { label: "Comisión contratación #1042", date: "14 sep 2026", amount: "-$15.300" },
  { label: "Retiro a cuenta bancaria", date: "10 sep 2026", amount: "-$120.000" },
  { label: "Pago contratación #1038", date: "08 sep 2026", amount: "+$85.000" },
]

// Notas de accesibilidad: cada pantalla mantiene visible el estado del sistema, previene errores
// con validación antes de avanzar y prioriza reconocimiento (etiquetas, estados y breadcrumbs).
export const navigationTree = [
  { title: "Inicio", items: ["Landing", "Selección de rol", "Registro", "Verificación OTP", "Cobertura"] },
  { title: "Demandante", items: ["Buscar", "Resultados", "Prestador", "Chat", "Contratación", "Seguimiento", "Pago", "Calificar", "Incidente"] },
  { title: "Prestador", items: ["Dashboard", "Solicitudes", "Chat", "Check-in / Check-out", "Verificación", "Plan Pro", "Billetera", "Reseñas"] },
]
