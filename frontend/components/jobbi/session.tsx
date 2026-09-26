"use client"

import { createContext, useContext } from "react"

import type { PerfilPrestador, Sesion } from "@/lib/api"

/** Lo que el usuario está mirando en este momento dentro de una pantalla. */
export type Seleccion = {
  prestadorId?: string
  contratacionId?: string
  conversacionId?: string
  categoriaId?: string
  /** Búsqueda de la que salió el prestador elegido: el contacto la cita como origen. */
  busquedaId?: string
}

type Contexto = {
  sesion: Sesion | null
  seleccion: Seleccion
  seleccionar: (cambios: Seleccion) => void
  /** Refresca el perfil de prestador en la sesión (p. ej. al cambiar de plan). */
  actualizarPrestador: (perfil: PerfilPrestador) => void
  salir: () => void
}

export const SesionContext = createContext<Contexto>({
  sesion: null,
  seleccion: {},
  seleccionar: () => {},
  actualizarPrestador: () => {},
  salir: () => {},
})

export const useSesion = () => useContext(SesionContext)

const CLAVE = "jobbi.sesion"

// La sesión se guarda en el navegador solo para que recargar la página no
// obligue a entrar de nuevo. No es un token ni da acceso a nada: el backend no
// la valida. Cualquier fallo al leerla (modo privado, almacenamiento bloqueado)
// se trata como "no hay sesión".
export function leerSesionGuardada(): Sesion | null {
  try {
    const crudo = window.localStorage.getItem(CLAVE)
    return crudo ? (JSON.parse(crudo) as Sesion) : null
  } catch {
    return null
  }
}

export function guardarSesion(sesion: Sesion | null) {
  try {
    if (sesion) window.localStorage.setItem(CLAVE, JSON.stringify(sesion))
    else window.localStorage.removeItem(CLAVE)
  } catch {
    // Sin almacenamiento la app sigue funcionando; solo no recuerda la sesión.
  }
}

/** Id del perfil activo según el rol, que es la clave de casi toda consulta. */
export const perfilIdDe = (sesion: Sesion | null) =>
  sesion?.rol === "Prestador" ? sesion.perfilPrestador?.id : sesion?.perfilDemandante?.id
