/** @type {import('next').NextConfig} */

// El reenvío del navegador hacia el API Gateway vive en app/api/[...ruta]/route.ts,
// no aquí: con `output: standalone` los rewrites de este archivo se serializan
// durante el build y la URL del gateway quedaría fija dentro de la imagen.
const nextConfig = {
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
