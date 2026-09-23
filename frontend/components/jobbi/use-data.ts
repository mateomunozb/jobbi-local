"use client"

import { useCallback, useEffect, useState } from "react"

import { ApiError } from "@/lib/api"

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
 */
export function useDatos<T>(
  consulta: () => Promise<T>,
  claves: ReadonlyArray<string | number | boolean | null | undefined>,
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

  const recargar = useCallback(() => setIntento(n => n + 1), [])
  return { datos, cargando, error, recargar }
}
