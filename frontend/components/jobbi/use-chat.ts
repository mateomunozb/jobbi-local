"use client"

import { useCallback, useEffect, useRef, useState } from "react"

import { ApiError, api, type Conversacion, type Mensaje } from "@/lib/api"

/**
 * Chat en tiempo real de una conversación, por WebSocket.
 *
 * El navegador abre /ws/comunicacion/conversaciones/{id} en su mismo origen;
 * Next lo reenvía al gateway y el gateway al servicio de Comunicación, dueño de
 * la sala. El historial se carga por HTTP y se vuelve a pedir en cada
 * reconexión, para recuperar lo que llegó mientras no había conexión.
 */

export type EstadoConexion = "conectando" | "conectado" | "reconectando" | "sin-conexion"

type Evento =
  | { tipo: "conectado"; estado: string; enLinea: number }
  | { tipo: "presencia"; enLinea: number }
  | { tipo: "mensaje"; mensaje: Mensaje }
  | { tipo: "escribiendo"; usuarioId: string }
  | { tipo: "cerrada"; conversacion: Conversacion }
  | { tipo: "error"; detalle: string; codigo: number }

const ESPERA_MAXIMA_MS = 10000
const AVISO_ESCRIBIENDO_MS = 2000

export function useChatEnVivo(
  conversacionId: string | undefined,
  usuarioId: string | undefined,
  { alCerrarse }: { alCerrarse?: () => void } = {},
) {
  const [mensajes, setMensajes] = useState<Mensaje[]>([])
  const [cargando, setCargando] = useState(true)
  const [conexion, setConexion] = useState<EstadoConexion>("conectando")
  const [enLinea, setEnLinea] = useState(0)
  const [otroEscribiendo, setOtroEscribiendo] = useState(false)
  const [cerrada, setCerrada] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const socket = useRef<WebSocket | null>(null)
  const ultimoAviso = useRef(0)
  const alCerrarseRef = useRef(alCerrarse)
  alCerrarseRef.current = alCerrarse

  // Los mensajes llegan por dos caminos (historial y WebSocket): se unen por id.
  const agregar = useCallback((nuevos: Mensaje[]) => setMensajes(previos => {
    const porId = new Map(previos.map(m => [m.id, m]))
    for (const m of nuevos) porId.set(m.id, m)
    return [...porId.values()].sort((a, b) => a.fechaEnvio.localeCompare(b.fechaEnvio))
  }), [])

  useEffect(() => {
    if (!conversacionId || !usuarioId) return
    let vivo = true
    let intento = 0
    let reintento: ReturnType<typeof setTimeout> | undefined
    let apagarEscribiendo: ReturnType<typeof setTimeout> | undefined

    setMensajes([]); setCargando(true); setCerrada(false); setEnLinea(0); setError(null); setOtroEscribiendo(false)

    const historial = () => api.mensajes(conversacionId)
      .then(pagina => { if (vivo) agregar(pagina.items) })
      .catch(() => { if (vivo) setError("No pudimos cargar el historial del chat.") })
      .finally(() => { if (vivo) setCargando(false) })

    const alRecibir = (evento: Evento) => {
      switch (evento.tipo) {
        case "conectado":
          setEnLinea(evento.enLinea)
          if (evento.estado === "CERRADA") setCerrada(true)
          break
        case "presencia":
          setEnLinea(evento.enLinea)
          break
        case "mensaje":
          agregar([evento.mensaje])
          if (evento.mensaje.remitenteId !== usuarioId) setOtroEscribiendo(false)
          break
        case "escribiendo":
          setOtroEscribiendo(true)
          clearTimeout(apagarEscribiendo)
          apagarEscribiendo = setTimeout(() => setOtroEscribiendo(false), 3000)
          break
        case "cerrada":
          setCerrada(true)
          alCerrarseRef.current?.()
          break
        case "error":
          setError(evento.detalle)
          if (evento.codigo === 409) setCerrada(true)
          break
      }
    }

    const conectar = () => {
      const protocolo = window.location.protocol === "https:" ? "wss" : "ws"
      const ws = new WebSocket(`${protocolo}://${window.location.host}/ws/comunicacion/conversaciones/`
        + `${conversacionId}?usuarioId=${encodeURIComponent(usuarioId)}`)
      socket.current = ws
      setConexion(intento === 0 ? "conectando" : "reconectando")

      ws.onopen = () => {
        intento = 0
        setConexion("conectado")
        setError(null)
        historial()
      }
      ws.onmessage = e => {
        try { alRecibir(JSON.parse(e.data)) } catch { /* evento ilegible: se ignora */ }
      }
      ws.onclose = e => {
        if (!vivo) return
        socket.current = null
        if (e.code === 4404) {
          setConexion("sin-conexion")
          setError("Esta conversación ya no existe.")
          return
        }
        // Se cayó la conexión (backend reiniciándose, red): reintento con
        // espera creciente, hasta 10 s entre intentos.
        intento += 1
        setConexion("reconectando")
        reintento = setTimeout(conectar, Math.min(1000 * 2 ** (intento - 1), ESPERA_MAXIMA_MS))
      }
    }

    historial()
    conectar()
    return () => {
      vivo = false
      clearTimeout(reintento)
      clearTimeout(apagarEscribiendo)
      socket.current?.close()
      socket.current = null
    }
  }, [conversacionId, usuarioId, agregar])

  /** Envía por WebSocket; si no hay conexión, por HTTP (llega igual a la sala). */
  const enviar = useCallback(async (contenido: string) => {
    setError(null)
    const ws = socket.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ tipo: "mensaje", contenido }))
      return
    }
    if (!conversacionId || !usuarioId) return
    try {
      agregar([await api.enviarMensaje(conversacionId, usuarioId, contenido)])
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "No pudimos enviar el mensaje.")
      throw e
    }
  }, [conversacionId, usuarioId, agregar])

  /** Avisa a la otra parte que se está escribiendo (como mucho cada 2 s). */
  const avisarEscribiendo = useCallback(() => {
    const ws = socket.current
    const ahora = Date.now()
    if (!ws || ws.readyState !== WebSocket.OPEN || ahora - ultimoAviso.current < AVISO_ESCRIBIENDO_MS) return
    ultimoAviso.current = ahora
    ws.send(JSON.stringify({ tipo: "escribiendo" }))
  }, [])

  return { mensajes, cargando, conexion, enLinea, otroEscribiendo, cerrada, error, enviar, avisarEscribiendo }
}
