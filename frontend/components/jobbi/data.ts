// Estructura de navegación del prototipo.
//
// Los datos de negocio (prestadores, contrataciones, mensajes, movimientos)
// ya no viven aquí: vienen del API Gateway a través de `lib/api.ts`. Lo que
// queda es la forma de la aplicación, que no depende del backend.

export type Role = "Demandante" | "Prestador" | "Admin"
export type Screen =
  | "map"
  | "landing"
  | "role"
  | "register"
  | "login"
  | "coverage"
  | "search"
  | "results"
  | "provider"
  | "chat"
  | "tracking"
  | "rating"
  | "incident"
  | "notifications"
  | "profile"
  | "dashboard"
  | "admin"
  | "requests"
  | "checkin"
  | "verification"
  | "plans"
  | "wallet"
  | "reviews"

export const navDemandante = [
  ["search", "Buscar"], ["chat", "Mensajes"], ["tracking", "Contrataciones"], ["notifications", "Notificaciones"], ["profile", "Perfil"],
] as const
export const navPrestador = [
  ["dashboard", "Dashboard"], ["requests", "Solicitudes"], ["chat", "Mensajes"], ["wallet", "Billetera"], ["profile", "Perfil"],
] as const
export const navAdmin = [
  ["admin", "Métricas"], ["notifications", "Alertas"], ["profile", "Perfil"],
] as const

// Notas de accesibilidad: cada pantalla mantiene visible el estado del sistema, previene errores
// con validación antes de avanzar y prioriza reconocimiento (etiquetas, estados y breadcrumbs).
export const navigationTree = [
  { title: "Inicio", items: ["Landing", "Selección de rol", "Registro", "Inicio de sesión", "Cobertura"] },
  { title: "Demandante", items: ["Buscar", "Resultados", "Prestador", "Chat", "Contrataciones", "Seguimiento", "Calificar", "Incidente"] },
  { title: "Prestador", items: ["Dashboard", "Solicitudes", "Chat", "Check-in / Check-out", "Verificación", "Plan Pro", "Billetera", "Reseñas"] },
]
