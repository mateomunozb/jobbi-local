"use client"

import { useCallback, useEffect, useState } from "react"

import { ApiError } from "@/lib/api"

/**
 * Cada cuánto se refrescan las pantallas "en vivo" (solicitudes, chat,
 * seguimiento, avisos). Es sondeo, no WebSocket: una consulta liviana cada pocos
 * segundos basta para que las dos partes vean lo que hace la otra.
 */
export const EN_VIVO_MS = 3000

export type EstadoCarga<T> = {
  datos: T | null
  cargando: boolean
  error: string | null
  recargar: () => void
}

/**
 * Ejecuta una consulta al backend y expone los tres estados que el prototipo ya
 * sabía dibujar: cargando (esqueletos), error (con reintento) y datos.
 *
 * `claves` cumple el papel de las dependencias de useEffect: cuando cambian, se
 * vuelve a consultar. Si alguna es null o undefined la consulta no se dispara,
 * que es lo que hace falta mientras aún no se conoce el id de la sesión.
 *
 * Con `cadaMs` la consulta se repite en segundo plano sin volver a mostrar los
 * esqueletos: es lo que usan las vistas que siguen algo asíncrono (un cobro que
 * llega por evento, el estado de las colas). Con `null` deja de sondear.
 */
export function useDatos<T>(
  consulta: () => Promise<T>,
  claves: ReadonlyArray<string | number | boolean | null | undefined>,
  { cadaMs = null }: { cadaMs?: number | null } = {},
): EstadoCarga<T> {
  const [datos, setDatos] = useState<T | null>(null)
  const [cargando, setCargando] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [intento, setIntento] = useState(0)

  const listo = claves.every(clave => clave !== null && clave !== undefined)

  useEffect(() => {
    if (!listo) {
      setCargando(false)
      return
    }
    // Evita que una respuesta lenta de una consulta anterior pise a la actual.
    let vigente = true
    setCargando(true)
    setError(null)
    consulta()
      .then(resultado => { if (vigente) { setDatos(resultado); setCargando(false) } })
      .catch((e: unknown) => {
        if (!vigente) return
        setError(e instanceof ApiError ? e.message : "No pudimos conectar con el servidor.")
        setCargando(false)
      })
    return () => { vigente = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...claves, intento, listo])

  useEffect(() => {
    if (!listo || !cadaMs) return
    let vigente = true
    const id = setInterval(() => {
      // Con la pestaña oculta no se consulta: nadie está mirando.
      if (typeof document !== "undefined" && document.hidden) return
      consulta()
        .then(resultado => { if (vigente) { setDatos(resultado); setError(null) } })
        // Un sondeo fallido no borra lo que ya se mostraba: el siguiente lo corrige.
        .catch(() => {})
    }, cadaMs)
    return () => { vigente = false; clearInterval(id) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...claves, listo, cadaMs])

  const recargar = useCallback(() => setIntento(n => n + 1), [])
  return { datos, cargando, error, recargar }
}
