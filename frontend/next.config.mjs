/** @type {import('next').NextConfig} */

// El reenvío HTTP del navegador hacia el API Gateway vive en
// app/api/[...ruta]/route.ts, no aquí: con `output: standalone` los rewrites de
// este archivo se serializan durante el build y la URL del gateway quedaría fija
// dentro de la imagen.
//
// La excepción es el WebSocket del chat: un Route Handler no puede mantener una
// conexión abierta, pero el servidor de Next sí reenvía el "upgrade" de un
// rewrite hacia una URL externa. Por eso /ws/* va por rewrite, y su destino se
// fija al construir: en la imagen, la URL interna del gateway en el clúster
// (ver Dockerfile); en `pnpm dev`, la de API_GATEWAY_URL al arrancar.
const gateway = process.env.API_GATEWAY_URL || "http://localhost:8080"

const nextConfig = {
  async rewrites() {
    return {
      beforeFiles: [{ source: "/ws/:ruta*", destination: `${gateway}/ws/:ruta*` }],
    }
  },
  // Salida autocontenida para construir la imagen de contenedor del frontend
  output: "standalone",
  typescript: {
    ignoreBuildErrors: true,
  },
  images: {
    unoptimized: true,
  },
}

export default nextConfig
