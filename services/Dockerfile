# Imagen única para todos los microservicios de dominio y el gateway.
#
# Un solo artefacto reduce el tiempo de build y garantiza que todos los
# contextos corran exactamente la misma versión de dependencias. La frontera
# entre contextos la impone el despliegue: cada Deployment de Kubernetes arranca
# un módulo distinto (SERVICE_MODULE) en su propio Pod, con su propio Service.
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY gateway/ ./gateway/
COPY identidad/ ./identidad/
COPY mercado/ ./mercado/
COPY contrataciones/ ./contrataciones/
COPY comunicacion/ ./comunicacion/
COPY confianza/ ./confianza/
COPY monetizacion/ ./monetizacion/
COPY soporte/ ./soporte/
COPY adquisicion/ ./adquisicion/
COPY proteccion/ ./proteccion/

# Se sobrescriben por Deployment; los valores por defecto levantan el gateway.
ENV SERVICE_MODULE=gateway \
    PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn ${SERVICE_MODULE}.main:app --host 0.0.0.0 --port ${PORT}"]
