# Etapa 1: dependencias
FROM node:22-alpine AS deps
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# Etapa 2: build de Next.js (output: standalone)
FROM node:22-alpine AS builder
WORKDIR /app
RUN corepack enable
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# Destino del WebSocket del chat (/ws/* en next.config.mjs). Queda fijo en la
# imagen al construirla: es la URL del gateway dentro del clúster.
ARG API_GATEWAY_URL=http://api-gateway.aws-local.svc.cluster.local:8080
ENV API_GATEWAY_URL=$API_GATEWAY_URL
RUN pnpm build

# Etapa 3: runtime mínimo
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000

CMD ["node", "server.js"]
