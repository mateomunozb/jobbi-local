import type { Metadata, Viewport } from "next"
import "./globals.css"

export const metadata: Metadata = {
  title: "JOBBI — Oficios confiables, cerca de ti",
  description: "Conecta con prestadores verificados en Medellín y el Valle de Aburrá.",
}

export const viewport: Viewport = { themeColor: "#0c7f82", width: "device-width", initialScale: 1, userScalable: false }

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="es"><body>{children}</body></html>
}
