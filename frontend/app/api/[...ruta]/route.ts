import { NextRequest, NextResponse } from "next/server"

/**
 * Proxy del navegador hacia el API Gateway.
 *
 * El cliente siempre llama a `/api/...` en el mismo origen y este handler
 * reenvía la petición desde el servidor de Next. Así el frontend no necesita
 * CORS, no expone la topología del clúster al navegador, y no hay que
 * reconstruir la imagen para apuntar a otro backend.
 *
 * Se resuelve con un Route Handler y no con `rewrites` de next.config porque
 * con `output: standalone` los rewrites se serializan durante el build: la URL
 * del gateway quedaría horneada en la imagen. Aquí `process.env` se lee en cada
 * petición, que es lo que permite configurarlo desde el Deployment.
 */

export const dynamic = "force-dynamic"

const gateway = () => process.env.API_GATEWAY_URL || "http://localhost:8080"

async function reenviar(request: NextRequest, segmentos: string[]) {
  const destino = `${gateway()}/api/${segmentos.join("/")}${request.nextUrl.search}`
  const conCuerpo = request.method !== "GET" && request.method !== "HEAD"

  let respuesta: Response
  try {
    respuesta = await fetch(destino, {
      method: request.method,
      headers: { "Content-Type": request.headers.get("content-type") ?? "application/json" },
      body: conCuerpo ? await request.text() : undefined,
      cache: "no-store",
    })
  } catch {
    // El gateway no respondió: se traduce a un 503 con un mensaje que la
    // interfaz ya sabe mostrar, en vez de dejar caer la petición.
    return NextResponse.json(
      { detail: "No pudimos contactar el API Gateway. Verifica que el backend esté arriba." },
      { status: 503 },
    )
  }

  const cuerpo = await respuesta.text()
  return new NextResponse(cuerpo, {
    status: respuesta.status,
    headers: { "Content-Type": respuesta.headers.get("content-type") ?? "application/json" },
  })
}

type Contexto = { params: Promise<{ ruta: string[] }> }

export async function GET(request: NextRequest, contexto: Contexto) {
  return reenviar(request, (await contexto.params).ruta)
}

export async function POST(request: NextRequest, contexto: Contexto) {
  return reenviar(request, (await contexto.params).ruta)
}
