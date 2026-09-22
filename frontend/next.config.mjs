/** @type {import('next').NextConfig} */
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
