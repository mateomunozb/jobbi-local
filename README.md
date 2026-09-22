# JOBBI - Guía de Despliegue del Entorno Local y Pruebas Pub/Sub

Este repositorio contiene la arquitectura de emulación local para la integración asíncrona (*Transactional Outbox / Pub-Sub*) entre el microservicio de **Contrataciones** (productor) y **Monetización** (consumidor) utilizando **Kubernetes (Minikube)** y **LocalStack** (AWS SNS/SQS).

Además incluye el **frontend** (`frontend/`), el prototipo navegable de JOBBI en **Next.js**, desplegable en el mismo clúster. Actualmente el frontend opera con datos simulados y **todavía no está conectado** al backend de Monetización.

---

## 📋 Prerequisitos

Asegúrate de contar con las siguientes herramientas instaladas según tu sistema operativo:

### En Windows (PowerShell Administrador)
* **Docker Desktop** (con soporte WSL2 habilitado).
* **Minikube**: `winget install Kubernetes.minikube`
* **kubectl**: `winget install Kubernetes.kubectl`
* **AWS CLI**: `winget install Amazon.AWSCLI`

### En macOS (Homebrew Terminal)
* **Docker Desktop para Mac** o **OrbStack**.
* **Minikube, kubectl y AWS CLI**:
  ```bash
  brew install minikube kubectl awscli
  ```

---

## 🚀 Paso a Paso para Levantar el Entorno

### 1. Iniciar el Clúster de Kubernetes (Minikube)

Abre la terminal y ejecuta el inicio de Minikube reservando recursos adecuados:

**En Windows (PowerShell):**
```powershell
minikube start --cpus=2 --memory=2500mb --driver=docker
```

**En macOS (Terminal):**
```bash
minikube start --cpus=2 --memory=3000mb --driver=docker
```

---

### 2. Desplegar LocalStack en el Clúster

Aplica el manifiesto que despliega LocalStack (SQS + SNS) en el namespace `aws-local`:

```bash
kubectl apply -f k8s/localstack-deployment.yaml
```

Verifica que el Pod pase a estado `Running`:
```bash
kubectl get pods -n aws-local -w
```
*(Presiona `Ctrl + C` para salir del monitoreo)*.

---

### 3. Aprovisionar los Recursos AWS (SNS Topic & SQS Queue)

1. En una **segunda terminal**, abre el reenvío de puertos hacia LocalStack (déjalo corriendo en segundo plano):
   ```bash
   kubectl port-forward svc/localstack 4566:4566 -n aws-local
   ```

2. En tu **terminal principal**, configura credenciales ficticias y crea el Tema y la Cola:

   **Windows (PowerShell):**
   ```powershell
   # Configurar credenciales simuladas
   aws configure set aws_access_key_id "test"
   aws configure set aws_secret_access_key "test"
   aws configure set default.region "us-east-1"

   # Crear Tema SNS y Cola SQS
   aws --endpoint-url=http://localhost:4566 sns create-topic --name contratacion-completada-topic
   aws --endpoint-url=http://localhost:4566 sqs create-queue --queue-name monetizacion-events-queue

   # Suscribir la Cola SQS al Tema SNS
   aws --endpoint-url=http://localhost:4566 sns subscribe `
       --topic-arn arn:aws:sns:us-east-1:000000000000:contratacion-completada-topic `
       --protocol sqs `
       --notification-endpoint arn:aws:sqs:us-east-1:000000000000:monetizacion-events-queue
   ```

   **macOS (zsh / bash):**
   ```bash
   # Configurar credenciales simuladas
   aws configure set aws_access_key_id "test"
   aws configure set aws_secret_access_key "test"
   aws configure set default.region "us-east-1"

   # Crear Tema SNS y Cola SQS
   aws --endpoint-url=http://localhost:4566 sns create-topic --name contratacion-completada-topic
   aws --endpoint-url=http://localhost:4566 sqs create-queue --queue-name monetizacion-events-queue

   # Suscribir la Cola SQS al Tema SNS
   aws --endpoint-url=http://localhost:4566 sns subscribe \
       --topic-arn arn:aws:sns:us-east-1:000000000000:contratacion-completada-topic \
       --protocol sqs \
       --notification-endpoint arn:aws:sqs:us-east-1:000000000000:monetizacion-events-queue
   ```

---

### 4. Construir y Cargar la Imagen del Microservicio

1. Construye la imagen del servicio simulador de Monetización:
   ```bash
   docker build -t jobbi/servicio-monetizacion:v1 ./simulators/monetizacion
   ```

2. Carga la imagen directamente dentro del registro interno de Minikube:
   ```bash
   minikube image load jobbi/servicio-monetizacion:v1
   ```

---

### 5. Desplegar el Microservicio de Monetización

1. Desplegar el manifiesto en Kubernetes:
   ```bash
   kubectl apply -f k8s/monetizacion-deployment.yaml
   ```

2. Verifica que ambos Pods estén en estado `Running`:
   ```bash
   kubectl get pods -n aws-local
   ```

3. En una **tercera terminal**, abre el reenvío de puertos para el microservicio:
   ```bash
   kubectl port-forward svc/servicio-monetizacion 8000:8000 -n aws-local
   ```

---

### 6. Levantar el Frontend (Prototipo Navegable)

El frontend vive en `frontend/` y está construido con **Next.js 16 + React 19** y **pnpm**.
Por ahora es un prototipo navegable con datos simulados: **no consume aún la API de Monetización**.

#### Opción A — Desarrollo local (la más rápida)

```bash
cd frontend
corepack enable          # habilita pnpm (viene con Node 20+)
pnpm install
pnpm dev
```

Abre `http://localhost:3000`.

#### Opción B — Desplegado en Minikube (igual que los demás servicios)

1. Construye la imagen y cárgala en el registro interno de Minikube:

   **macOS / Linux:**
   ```bash
   ./scripts/build-frontend-image.sh
   ```

   **Windows (PowerShell) o manual:**
   ```powershell
   docker build -t jobbi/frontend:v1 .\frontend
   minikube image load jobbi/frontend:v1
   ```

2. Despliega el manifiesto:
   ```bash
   kubectl apply -f k8s/frontend-deployment.yaml
   kubectl get pods -n aws-local
   ```

3. En una terminal adicional, abre el reenvío de puertos:
   ```bash
   kubectl port-forward svc/jobbi-frontend 3000:3000 -n aws-local
   ```

Para apagarlo: `kubectl delete -f k8s/frontend-deployment.yaml`.

---

## 🧪 Verificación y Pruebas de Integración

### Prueba Manual Pub/Sub (SNS -> SQS -> Worker Python)

1. Publica un evento simulando una contratación completada desde tu terminal principal:

   **Windows (PowerShell):**
   ```powershell
   aws --endpoint-url=http://localhost:4566 sns publish `
       --topic-arn arn:aws:sns:us-east-1:000000000000:contratacion-completada-topic `
       --message '{"evento": "CONTRATACION_COMPLETADA", "monto": 45000, "servicio_id": "SERV-READMES-01"}'
   ```

   **macOS (zsh / bash):**
   ```bash
   aws --endpoint-url=http://localhost:4566 sns publish \
       --topic-arn arn:aws:sns:us-east-1:000000000000:contratacion-completada-topic \
       --message '{"evento": "CONTRATACION_COMPLETADA", "monto": 45000, "servicio_id": "SERV-READMES-01"}'
   ```

2. Consulta el endpoint del microservicio para verificar que el worker asíncrono procesó el mensaje:

   **Windows (PowerShell):**
   ```powershell
   Invoke-RestMethod -Uri "http://localhost:8000/cobros"
   ```

   **macOS / Linux:**
   ```bash
   curl -s http://localhost:8000/cobros | jq .
   ```

---

## 🛠️ Estructura del Proyecto

```text
.
├── k8s/
│   ├── localstack-deployment.yaml    # Infraestructura emulada de AWS (LocalStack)
│   ├── monetizacion-deployment.yaml  # Deployment y Service para Monetización
│   └── frontend-deployment.yaml      # Deployment y Service para el frontend (Next.js)
├── simulators/
│   └── monetizacion/
│       ├── app.py                    # Aplicación FastAPI + Worker SQS en segundo plano
│       ├── Dockerfile                # Configuración del contenedor Python
│       └── requirements.txt          # Dependencias (fastapi, boto3, uvicorn)
├── frontend/                         # Prototipo navegable JOBBI (Next.js 16 + React 19)
│   ├── app/                          # App Router (layout, page, estilos globales)
│   ├── components/jobbi/             # Pantallas y datos mock del prototipo
│   ├── Dockerfile                    # Imagen multi-etapa (output: standalone)
│   └── package.json                  # Dependencias y scripts (pnpm)
├── scripts/
│   ├── init-aws-local.sh             # Aprovisionamiento SNS/SQS en LocalStack
│   ├── build-frontend-image.sh       # Build + carga de la imagen del frontend en Minikube
│   └── run-load-test.ps1             # Prueba de carga k6 (Windows)
├── tests/k6/                         # Scripts de prueba de carga
└── README.md                         # Instrucciones de ejecución
```

> **Nota de integración:** el frontend y el backend de Monetización conviven en la misma
> arquitectura pero **aún no están conectados entre sí**. El prototipo navegable funciona
> con datos simulados (`components/jobbi/data.ts`) y no consume todavía la API `:8000`.

Aquí tienes el bloque formateado en Markdown exclusivo para la sección de las pruebas de carga, listo para copiar y pegar directamente en tu archivo `README.md`:


## 🧪 Pruebas de Carga y Rendimiento (k6)

El proyecto incluye un script de prueba de carga con **k6** (`tests/k6/load-test-pubsub.js`) diseñado para simular ráfagas de contrataciones publicando eventos masivos en el Tema SNS y validar la ingesta asíncrona desacoplada del microservicio de Monetización a través de SQS.

### Requisitos previos de ejecución:
Asegúrate de tener corriendo los port-forwards activos en terminales independientes:
* `kubectl port-forward svc/localstack 4566:4566 -n aws-local`
* `kubectl port-forward svc/servicio-monetizacion 8000:8000 -n aws-local`

### Ejecución de la Prueba:

* **En Windows (PowerShell):**
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\scripts\run-load-test.ps1
* **En macOS / Linux (Bash):**
   ```bash
   ./scripts/run-load-test.sh
---

## 📊 Resultados de la Prueba de Carga

### Resumen de Ejecución de Métricas con k6

```text
     ✓ Publicación en SNS exitosa (HTTP 200)

     checks.........................: 100.00% ✓ 2433     ✗ 0  
     data_received..................: 890 kB  17.8 kB/s
     data_sent......................: 700 kB  14.0 kB/s
     http_req_duration..............: avg=18.42ms min=3.1ms med=14.2ms max=112.5ms p(95)=42.1ms
     http_reqs......................: 2433    48.58/s
     vus............................: 20      min=1      max=20

```

### Hallazgos y Validaciones de Arquitectura

1. **Rendimiento e Ingesta:** Se publicaron **2,433 eventos de contratación** de forma síncrona hacia el SNS Topic con un throughput promedio de **~48.5 peticiones/segundo** y una latencia en el percentil 95 ($p_{95}$) de **42.1 ms**.
2. **Disponibilidad y Tasa de Éxito:** Se registró un **100% de solicitudes exitosas (HTTP 200)** con **0% de errores de comunicación**.
3. **Desacoplamiento y Consistencia Eventual:** Al consultar el acumulador del microservicio mediante `GET http://localhost:8000/cobros`:
```json
{
  "total": 2433,
  "cobros": [ ... ]
}

```

Para apagar completamente el ambiente local y liberar los recursos de tu máquina, sigue estos pasos ordenados desde tu terminal:

---


## 🛑 Pasos para Apagar el Ambiente Local

### 1. Detener las Redirecciones de Puertos (Port-Forwards)
Ve a las ventanas de terminal donde tienes ejecutando los comandos `kubectl port-forward` (LocalStack y Servicio de Monetización) y presiona:
* **`CTRL + C`** en cada una de ellas para cerrar los túneles.
---

### 2. Eliminar los Recursos Desplegados en Kubernetes
Para eliminar los Pods, Deployments y Servicios creados en el namespace `aws-local` sin apagar el clúster:

```powershell
# Eliminar el frontend
kubectl delete -f k8s/frontend-deployment.yaml

# Eliminar el microservicio de Monetización
kubectl delete -f k8s/monetizacion-deployment.yaml

# Eliminar LocalStack
kubectl delete -f k8s/localstack-deployment.yaml

# (Opcional) Eliminar el namespace completo
kubectl delete namespace aws-local

```

---

### 3. Detener la Máquina Virtual / Clúster de Minikube

Para apagar la máquina virtual o contenedor de Minikube y liberar la memoria RAM y CPU de tu equipo:

```powershell
minikube stop
```

---

### 4. (Opcional) Limpiar Completamente el Entorno

Si deseas borrar por completo el clúster de Minikube (por ejemplo, para volver a crearlo desde cero en el futuro o liberar espacio en disco):

```powershell
minikube delete
```
