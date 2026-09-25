# JOBBI - Guía de Despliegue del Entorno Local y Pruebas Pub/Sub

Este repositorio contiene la arquitectura de emulación local para la integración asíncrona (*Transactional Outbox / Pub-Sub*) entre el microservicio de **Contrataciones** (productor) y **Monetización** (consumidor) utilizando **Kubernetes (Minikube)** y **LocalStack** (AWS SNS/SQS).

Además incluye el **backend de microservicios** (`services/`), con un servicio FastAPI por contexto delimitado del modelo de dominio y un **API Gateway** como punto de entrada, y el **frontend** (`frontend/`) en **Next.js**, ya **conectado al backend**: registro, inicio de sesión y todas las pantallas consumen datos reales del clúster.

> **Pub/Sub:** al hacer check-out, Contrataciones guarda el evento
> `CONTRATACION_COMPLETADA` en su *outbox* y lo publica en SNS. Monetización lo
> consume de su cola SQS y cobra la comisión. Cómo está hecho y cómo probarlo
> (frontend, pruebas automatizadas, carga y estrés con k6):
> **[docs/pubsub.md](docs/pubsub.md)**.

---

## ⚡ Arranque rápido

Con Minikube ya iniciado, un solo comando construye las imágenes y despliega
backend y frontend:

```bash
minikube start --cpus=2 --memory=3000mb --driver=docker
./scripts/deploy-all.sh
```

Luego abre dos túneles, cada uno en su terminal:

```bash
kubectl port-forward svc/jobbi-frontend 3000:3000 -n aws-local
kubectl port-forward svc/api-gateway 8080:8080 -n aws-local
```

Entra a `http://localhost:3000`. **El sistema arranca sin ninguna cuenta ni
ningún dato**: no hay usuarios de ejemplo. Crea la primera desde "Crear una
cuenta". Para ver el flujo completo, registra primero un **prestador** (declara
su oficio y categoría) y luego un **demandante**, que ya podrá encontrarlo,
abrir el chat y confirmar con él la tarifa del servicio.

Si ya habías probado antes y quieres partir de cero: `./scripts/reset-datos.sh`.

El resto del documento explica cada paso por separado.

---

## 🌱 Sin datos de ejemplo

La base es la **única fuente de verdad**. Al arrancar, las nueve bases están
vacías: no hay usuarios, ni contrataciones, ni catálogo escritos en el código.
Todo lo que existe llegó por el uso real de la aplicación.

Esto tiene una consecuencia que conviene conocer: **el catálogo de categorías y
oficios también empieza vacío**. Se construye solo, desde el registro de cada
prestador: escribe el oficio que ofrece y elige una categoría, y el contexto
Mercado crea la `Categoria` y el `Oficio` si aún no existen. Por eso el primer
prestador registrado es el que estrena el catálogo.

El orden natural para probar el sistema desde cero:

1. Registra un **prestador** → se crean su `Usuario`, su `PerfilPrestador`, y su
   `Categoria` + `Oficio` + `PrestadorOficio`.
2. Registra un **demandante** → ya ve al prestador en la búsqueda.
3. Pulsa **Contactar** → se abren el `Contacto` y la `Conversacion`.
4. Escribe un mensaje → se guarda como `Mensaje`.
5. **Confirma la tarifa** desde el chat → nace la `Contratacion` con su comisión
   congelada, y sigue el flujo de check-in, check-out y reseña.

### Volver a dejarlo todo vacío

```bash
./scripts/reset-datos.sh            # sobre el clúster
./scripts/reset-datos.sh --local    # sobre los archivos SQLite de .datos/
```

Vacía las nueve bases y reinicia los servicios, que recrean su esquema al
arrancar. Úsalo antes de una demostración en vivo.

> **Antes de una demo, no corras el smoke test.** Crea sus propias cuentas,
> oficios y conversaciones para verificar los endpoints, y esos datos quedan
> mezclados con los de la demostración.

---

## 🔐 Registro e inicio de sesión

> **Alcance acordado:** la validación de identidad con Truora se da por
> **aprobada siempre** y el inicio de sesión **solo verifica que el correo
> exista** — sin contraseña, sin token y sin OTP. Es deliberado, para no frenar
> el resto del trabajo. No es un esquema de autenticación real.

Estos dos endpoints son la puerta de entrada; hay otras escrituras para el
catálogo y el chat (ver la sección siguiente).

| Método | Ruta | Respuesta |
|---|---|---|
| POST | `/api/auth/registro` | `201` con la sesión · `409` si el correo ya existe · `422` si los datos no validan |
| POST | `/api/auth/login` | `200` con la sesión · `404` si el correo no existe |

Ambos devuelven la misma forma, que es lo que el frontend usa para decidir la
pantalla de destino:

```json
{
  "usuario": { "id": "...", "nombreCompleto": "Laura Restrepo Ochoa", "correo": "..." },
  "rol": "Demandante",
  "perfilDemandante": { "id": "...", "ubicacionPrincipal": { "municipio": "Medellín" } },
  "perfilPrestador": null,
  "verificado": true
}
```

- **Al registrarse** se crea el `Usuario` y se activa el perfil que corresponda
  al rol elegido. Si es prestador, nace con `insigniaVerificado: true` y
  `estadoVerificacionActual: APROBADA`.
- **Al entrar**, el rol sale de qué perfil tenga el usuario: si hay perfil de
  prestador va al dashboard, si no, a la pantalla de búsqueda.
- `verificado` viaja en la respuesta en vez de estar escrito en el cliente, para
  que el día que la verificación sea real solo cambie el backend.

Probarlo desde la terminal:

```bash
curl -s -X POST http://localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"correo":"<el correo que registraste>"}' | jq '.rol, .usuario.nombreCompleto'
```

Los datos se guardan en PostgreSQL, así que los usuarios que registres
sobreviven a los reinicios (ver la sección de persistencia).

---

## 💬 Escrituras: catálogo, contacto y chat

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/api/mercado/catalogo/oficio` | Registra un oficio, creando su categoría si no existe |
| POST | `/api/mercado/prestador-oficios` | Asocia un oficio a un prestador con su tarifa |
| POST | `/api/mercado/contactos` | Abre el contacto demandante ↔ prestador |
| POST | `/api/comunicacion/conversaciones` | Abre la conversación de un contacto |
| POST | `/api/comunicacion/mensajes` | Envía un mensaje |
| POST | `/api/soporte/incidentes` | Reporta un incidente sobre una contratación |
| POST | `/api/bff/contactar` | **Contacto + conversación en una sola llamada** |
| POST | `/api/bff/acuerdos` | Propone la tarifa del servicio desde el chat |
| POST | `/api/bff/acuerdos/{id}/aceptar` | Confirma la tarifa; con ambas partes, crea el servicio |
| POST | `/api/bff/contrataciones/{id}/check-in` | El prestador marca su llegada |
| POST | `/api/bff/contrataciones/{id}/check-out` | Cierra el servicio y **cobra la comisión** |
| POST | `/api/bff/resenas` | Publica la reseña y recalcula la reputación del perfil |
| POST | `/api/bff/prestadores/{id}/plan` | Cambia el plan (perfil en Identidad + suscripción en Monetización) |

### Cómo funciona "Contactar"

El botón del perfil del prestador no solo navega: llama a `/api/bff/contactar`,
que encadena dos contextos —el `Contacto` pertenece a Mercado y la
`Conversacion` a Comunicación— y devuelve el hilo listo para abrir el chat.

Ambas altas son **idempotentes**: volver a contactar a la misma persona reabre
el mismo hilo en lugar de duplicarlo.

```bash
curl -s -X POST http://localhost:8080/api/bff/contactar \
  -H 'Content-Type: application/json' \
  -d '{"demandanteId":"<id>","prestadorId":"<id>"}' | jq
```

### La bandeja de mensajes

`GET /api/bff/mensajes?demandanteId=<id>` (o `prestadorId`) compone la bandeja
desde tres contextos: los contactos vienen de Mercado, las conversaciones de
Comunicación y el nombre de la otra parte de Identidad.

Se pide **por contacto** y no por quién ha escrito, que es lo que hace visible
una conversación recién abierta cuando todavía no tiene ningún mensaje.

Cada hilo trae además el **acuerdo de tarifa vigente** y su contratación, que es
lo que le permite al chat saber si toca proponer, confirmar o entrar al servicio.

---

## 🤝 Del chat al servicio: tarifa, ejecución y comisión

El recorrido completo, tal como se hace en la aplicación:

1. **Registro.** Primero el prestador (declara oficio, categoría y tarifa), luego
   el demandante.
2. **Contacto.** El demandante encuentra al prestador y abre el chat.
3. **Confirmar tarifa.** En el chat aparece el panel de tarifa: una parte propone
   un valor y un medio de pago, la otra confirma. **El servicio nace de esa
   segunda confirmación**, no de la conversación.
4. **Ejecución.** El demandante ve la línea de etapas (solicitada → aceptada → en
   curso → check-in → check-out → completada). El prestador no ve esa línea: solo
   sus dos botones, **check-in** y **check-out**.
5. **Cierre.** El check-out completa el servicio y abre las reseñas. Como el pago
   fue en efectivo, el dinero nunca pasa por la plataforma: lo que se registra es
   la **comisión cargada a la billetera del prestador**, según su plan.

### La comisión se congela al cerrar el trato

El porcentaje se fija en el instante en que ambas partes confirman la tarifa, y
queda escrito en la contratación (`porcentajeComisionAplicado`). El acuerdo
guarda además con qué plan se cerró (`planPrestadorAlAcordar`).

Eso significa que **si el trato se hizo en Free, se cobra Free**, aunque el
prestador pase a Pro mientras el servicio está en curso; y el siguiente acuerdo
que cierre ya irá con la tarifa de Pro. Nada recalcula un acuerdo cerrado.

El porcentaje de cada plan lo fija Monetización y se consulta en
`GET /api/monetizacion/planes` (Free 18%, Pro 12%): no está escrito en el
frontend ni duplicado en otro contexto.

### Cambiar de plan en vivo

En **Perfil → Plan actual** el prestador cambia entre Free y Pro con un botón.
El cambio escribe en dos contextos a la vez —el plan del perfil en Identidad y
la suscripción en Monetización— y sirve justamente para ver, durante una
demostración, cómo se comporta el congelamiento:

```bash
# Comisión que se congelaría hoy para este prestador
curl -s http://localhost:8080/api/bff/comision-vigente/<prestadorId> | jq

# Cambiar el plan
curl -s -X POST http://localhost:8080/api/bff/prestadores/<prestadorId>/plan \
  -H 'Content-Type: application/json' -d '{"plan":"PRO"}' | jq
```

---

## 🗄️ Persistencia: una base de datos por contexto

Cada contexto delimitado tiene **su propia base de datos** dentro de una única
instancia de PostgreSQL (`StatefulSet` con un `PersistentVolumeClaim` de 1 Gi).

| Contexto | Base de datos |
|---|---|
| Identidad | `jobbi_identidad` |
| Mercado de Oficios | `jobbi_mercado` |
| Contrataciones | `jobbi_contrataciones` |
| Comunicación | `jobbi_comunicacion` |
| Confianza | `jobbi_confianza` |
| Monetización | `jobbi_monetizacion` |
| Soporte | `jobbi_soporte` |
| Adquisición | `jobbi_adquisicion` |
| Protección | `jobbi_proteccion` |

No es una separación por convención: **PostgreSQL no permite consultar entre
bases distintas**, así que un JOIN accidental entre contextos ni siquiera es
expresable. Compruébalo:

```bash
kubectl exec -n aws-local postgres-0 -- \
  psql -U jobbi -d jobbi_contrataciones -c "SELECT * FROM usuarios LIMIT 1;"
# ERROR:  relation "usuarios" does not exist
```

Cada Deployment recibe solo **su** cadena de conexión, desde el Secret
`postgres-urls`: ningún servicio tiene credenciales para la base de otro.

### Siembra idempotente

Al arrancar, cada servicio crea sus tablas si no existen y siembra los datos de
demostración **solo si están vacías**. Reiniciar un Pod no duplica nada ni pisa
lo que hayas creado. El esquema no evoluciona en este proyecto, por eso basta
`create_all`; un sistema en producción llevaría migraciones versionadas
(Alembic).

### Comprobar que persiste

```bash
# 1. Registra a alguien
curl -s -X POST http://localhost:8080/api/auth/registro \
  -H 'Content-Type: application/json' \
  -d '{"nombreCompleto":"Prueba Persistencia","correo":"prueba@example.com","telefono":"3001234567","numeroDocumento":"99001122","rol":"Demandante"}'

# 2. Borra el Pod entero
kubectl delete pod -n aws-local -l app=identidad
kubectl rollout status deployment/identidad -n aws-local

# 3. Sigue ahí
curl -s -X POST http://localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' -d '{"correo":"prueba@example.com"}' | jq '.usuario.nombreCompleto'
```

Para inspeccionar cualquier base directamente:

```bash
kubectl exec -it -n aws-local postgres-0 -- psql -U jobbi -d jobbi_identidad
```

Si quieres empezar de cero, borra el volumen (esto **elimina todos los datos**):

```bash
kubectl delete -f k8s/postgres-deployment.yaml
kubectl delete pvc datos-postgres-0 -n aws-local
```

### En local, sin PostgreSQL

`./scripts/run-backend-local.sh` no requiere instalar nada: si no hay
`DATABASE_URL`, cada contexto usa su propio archivo SQLite en `.datos/`. Un
archivo por contexto, de modo que la separación se mantiene igual.

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

> **Ya no hace falta hacerlo a mano.** LocalStack ejecuta
> `scripts/init-aws-local.sh` al arrancar (ConfigMap `localstack-init`) y crea
> el tema, la cola, la DLQ y la suscripción. El Pod queda *Ready* cuando termina.
> Si quieres repetirlo desde tu máquina, abre el port-forward de abajo y corre
> `./scripts/init-aws-local.sh`, que es seguro de correr varias veces. Los
> comandos manuales se conservan como referencia.

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

### 4. Construir y Cargar la Imagen del Backend

Una sola imagen contiene los 9 microservicios de dominio y el gateway; cada
Deployment arranca un módulo distinto. El contexto de Monetización incluido ahí
conserva el worker SQS del simulador original.

1. Construye la imagen y cárgala en el registro interno de Minikube:
   ```bash
   ./scripts/build-backend-image.sh
   ```

   **Windows (PowerShell) o manual:**
   ```powershell
   docker build -t jobbi/backend:v1 .\services
   minikube image load jobbi/backend:v1
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
Consume el backend a través del API Gateway: el registro, el inicio de sesión y
todas las pantallas (búsqueda, perfil, contrataciones, mensajes, billetera,
reseñas y métricas) muestran datos reales de los microservicios.

El frontend nunca habla directo con un microservicio: el navegador llama a
`/api/...` en su mismo origen y el servidor de Next reenvía al API Gateway
(`frontend/app/api/[...ruta]/route.ts`). Por eso no hace falta CORS y la URL del
gateway se cambia con la variable `API_GATEWAY_URL` sin reconstruir la imagen.

#### Opción A — Desarrollo local (la más rápida)

Necesita el backend arriba. Si está en Minikube, deja el túnel del gateway
abierto en otra terminal (`kubectl port-forward svc/api-gateway 8080:8080 -n aws-local`),
que es el valor por defecto de `API_GATEWAY_URL`:

```bash
cd frontend
corepack enable          # habilita pnpm (viene con Node 20+)
pnpm install
pnpm dev
```

Abre `http://localhost:3000`. No hay cuentas de prueba: créalas desde "Crear una
cuenta", primero la del prestador y después la del demandante.

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

   El manifiesto ya define `API_GATEWAY_URL` apuntando al gateway por DNS
   interno del clúster, así que el frontend queda conectado al backend.

3. En una terminal adicional, abre el reenvío de puertos:
   ```bash
   kubectl port-forward svc/jobbi-frontend 3000:3000 -n aws-local
   ```

Para apagarlo: `kubectl delete -f k8s/frontend-deployment.yaml`.

---

### 7. Levantar el Backend de Microservicios (API Gateway + 9 contextos)

El backend vive en `services/` y está construido con **FastAPI**. Cada contexto
delimitado del modelo de dominio es su propio microservicio, con su propio Pod y
su propio `Service`; un **API Gateway** es el único punto de entrada.

| Servicio | Contexto delimitado | Puerto |
|---|---|---|
| `identidad` | Identidad y Perfiles | 8001 |
| `mercado` | Mercado de Oficios | 8002 |
| `contrataciones` | Contrataciones | 8003 |
| `comunicacion` | Comunicación | 8004 |
| `confianza` | Confianza y Verificación | 8005 |
| `monetizacion` | Monetización (además consume SQS) | 8000 |
| `soporte` | Soporte y Disputas | 8006 |
| `adquisicion` | Adquisición y Distribución | 8007 |
| `proteccion` | Protección / Seguros *(reservado)* | 8008 |
| `api-gateway` | Punto de entrada y composición BFF | 8080 |

El catálogo completo de endpoints está en [`services/README.md`](services/README.md).

#### Opción A — Desplegado en Minikube (reproducible en cualquier máquina)

1. Construye la imagen única del backend y cárgala en Minikube:

   **macOS / Linux:**
   ```bash
   ./scripts/build-backend-image.sh
   ```

   **Windows (PowerShell):**
   ```powershell
   docker build -t jobbi/backend:v1 .\services
   minikube image load jobbi/backend:v1
   ```

2. Despliega los 9 microservicios y el gateway:

   **macOS / Linux:**
   ```bash
   ./scripts/deploy-backend.sh
   ```

   **Windows (PowerShell):**
   ```powershell
   kubectl apply -f k8s\domain-services.yaml
   kubectl apply -f k8s\monetizacion-deployment.yaml
   kubectl apply -f k8s\gateway-deployment.yaml
   kubectl get pods -n aws-local
   ```

3. Abre el gateway. En una **terminal adicional**, déjalo corriendo:

   ```bash
   kubectl port-forward svc/api-gateway 8080:8080 -n aws-local
   ```

   El gateway queda en `http://localhost:8080` (Swagger en `/docs`), igual que
   los port-forwards de LocalStack y Monetización.

> **Sobre el NodePort (30080):** el `Service` del gateway es de tipo NodePort
> para que el clúster no dependa de un Ingress Controller. En **Linux** puedes
> alcanzarlo directo en `http://$(minikube ip):30080`. En **macOS y Windows con
> el driver Docker** esa IP no es enrutable desde el host, y
> `minikube service api-gateway -n aws-local --url` abre un túnel que **bloquea
> la terminal** (por eso no sirve dentro de `$(...)`): en esas plataformas usa
> `kubectl port-forward`, como arriba.

> **Nota:** las consultas no necesitan LocalStack, pero **el cobro de comisiones
> sí**, porque viaja como evento. Por eso `deploy-backend.sh` también despliega
> LocalStack. Si LocalStack cae, el check-out sigue funcionando: los eventos
> esperan en el outbox de Contrataciones y se publican cuando vuelve.

#### Opción B — Local, sin Kubernetes (para desarrollar)

Levanta los procesos en tu máquina y crea el entorno virtual la primera vez.
También levanta LocalStack en un contenedor, así que el Pub/Sub funciona igual
que en el clúster. **Docker es obligatorio**: el cobro de comisiones solo existe
por eventos, no hay modo síncrono:

```bash
./scripts/run-backend-local.sh
```

Gateway en `http://localhost:8080/docs`. `Ctrl + C` detiene todo. Los logs de
cada servicio quedan en `.logs/`.

---

## 🧪 Verificación y Pruebas de Integración

### Prueba de Humo del Backend (29 endpoints, los 9 contextos)

Recorre un endpoint representativo de cada contexto a través del gateway,
incluidas las vistas compuestas BFF, y reporta el código HTTP de cada uno.

Para poder probar con las bases vacías **crea sus propias cuentas, oficios y
conversaciones**, que quedan guardadas. Es lo que se quiere al verificar el
backend, y lo que no se quiere justo antes de una demostración en vivo: si lo
corriste, deja el sistema limpio con `./scripts/reset-datos.sh`.

```bash
# Contra el backend local (./scripts/run-backend-local.sh)
./scripts/smoke-test-backend.sh

# Contra el gateway en Minikube: con el port-forward activo en otra terminal
#   kubectl port-forward svc/api-gateway 8080:8080 -n aws-local
./scripts/smoke-test-backend.sh

# Solo en Linux, alcanzando el NodePort directamente:
./scripts/smoke-test-backend.sh "http://$(minikube ip):30080"
```

Salida esperada: `=== Resultado: 29 correctos, 0 fallidos ===`.

### Consultas Manuales

Define primero la base del gateway:

```bash
BASE=http://localhost:8080        # o la URL de `minikube service api-gateway -n aws-local --url`
PRESTADOR=00000001-0000-4000-8000-0000000000b1
```

```bash
# 1. ¿Está todo el backend arriba? (estado de los 9 contextos)
curl -s $BASE/health/servicios | jq '.status, .disponibles'

# 2. Catálogo de contextos enrutados por el gateway
curl -s $BASE/api/servicios | jq '.servicios[].prefijoGateway'

# 3. Consulta a un solo contexto: prestadores verificados con 4.7+
curl -s "$BASE/api/identidad/prestadores?verificado=true&calificacionMinima=4.7" | jq '.items[].descripcion'

# 4. Línea de tiempo de una contratación en disputa
curl -s $BASE/api/contrataciones/contrataciones/00000003-0000-4000-8000-000000001051/timeline | jq '.pasos'

# 5. Composición BFF: ficha 360° del prestador (6 contextos en una llamada)
curl -s $BASE/api/bff/prestadores/$PRESTADOR | jq 'keys'

# 6. Tablero de métricas del rol Admin
curl -s $BASE/api/bff/admin/metricas | jq '.contrataciones, .incidentes.abiertos'
```

También puedes explorar todo desde Swagger: `$BASE/docs` para el gateway, y el
`/docs` de cada microservicio haciendo `kubectl port-forward` a su puerto.

### Prueba de punta a punta del Pub/Sub

```bash
./scripts/test-pubsub.sh                      # check-out → outbox → SNS → SQS → billetera
./scripts/test-pubsub.sh --consumidor-caido   # (Minikube) el evento espera a que vuelva Monetización
cd services && ../.venv/bin/python -m pytest  # pruebas automatizadas del patrón
```

Detalle y prueba desde el frontend en [docs/pubsub.md](docs/pubsub.md).

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

2. Consulta el endpoint del microservicio para verificar que el worker asíncrono procesó el mensaje (como no trae contratación, queda como `REGISTRADO_SIN_CONTRATACION`, sin tocar billeteras):

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
│   ├── localstack-deployment.yaml    # LocalStack + init hook que crea SNS, SQS y DLQ
│   ├── postgres-deployment.yaml      # PostgreSQL: una base por contexto + PVC
│   ├── domain-services.yaml          # 8 microservicios de dominio (Deployment + Service)
│   ├── monetizacion-deployment.yaml  # Monetización (además consumidor SQS)
│   ├── gateway-deployment.yaml       # API Gateway expuesto como NodePort 30080
│   └── frontend-deployment.yaml      # Deployment y Service para el frontend (Next.js)
├── services/                         # Backend FastAPI: un paquete por contexto delimitado
│   ├── common/                       # Enumeraciones, UUID de siembra, acceso a datos y fábrica de apps
│   ├── gateway/                      # Enrutamiento /api/* y composición BFF
│   ├── identidad/                    # Usuario, PerfilDemandante, PerfilPrestador
│   ├── mercado/                      # Categoría, Oficio, PrestadorOficio, Búsqueda, Contacto
│   ├── contrataciones/               # Contratación, su ciclo de vida y el outbox del evento
│   ├── comunicacion/                 # Conversación, Mensaje, Notificación
│   ├── confianza/                    # VerificaciónIdentidad, AliadoVerificación, Reseña
│   ├── monetizacion/                 # Pago, Suscripción, Billetera + worker SQS
│   ├── soporte/                      # Incidente
│   ├── adquisicion/                  # AliadoDistribución y referidos
│   ├── proteccion/                   # Aseguradora, PlanProtección (reservado)
│   ├── Dockerfile                    # Imagen única; SERVICE_MODULE elige el contexto
│   ├── requirements.txt              # fastapi, uvicorn, pydantic, httpx, boto3
│   └── README.md                     # Mapa de contextos y catálogo de endpoints
├── frontend/                         # Aplicación JOBBI (Next.js 16 + React 19)
│   ├── app/                          # App Router (layout, page, estilos globales)
│   ├── app/api/[...ruta]/route.ts    # Proxy del navegador hacia el API Gateway
│   ├── lib/api.ts                    # Cliente tipado de todos los endpoints
│   ├── components/jobbi/             # Pantallas, sesión y hook de carga de datos
│   ├── Dockerfile                    # Imagen multi-etapa (output: standalone)
│   └── package.json                  # Dependencias y scripts (pnpm)
├── scripts/
│   ├── init-aws-local.sh             # Aprovisionamiento SNS/SQS/DLQ (idempotente)
│   ├── test-pubsub.sh                # Prueba funcional de punta a punta del Pub/Sub
│   ├── deploy-all.sh                 # Build + despliegue de todo (backend y frontend)
│   ├── build-backend-image.sh        # Build + carga de la imagen del backend en Minikube
│   ├── deploy-backend.sh             # Despliegue de los 9 contextos + gateway
│   ├── run-backend-local.sh          # Backend completo en local, sin Kubernetes
│   ├── smoke-test-backend.sh         # Prueba de humo de 29 endpoints (crea datos propios)
│   ├── reset-datos.sh                # Vacía las nueve bases y reinicia los servicios
│   ├── build-frontend-image.sh       # Build + carga de la imagen del frontend en Minikube
│   ├── run-load-test.sh              # Carga / estrés / pico con k6 (macOS / Linux)
│   └── run-load-test.ps1             # Lo mismo en Windows
├── tests/k6/                         # e2e-checkout-pubsub.js, load-test-pubsub.js, lib/escenarios.js
├── docs/                             # tecnologias-y-flujo.md, pubsub.md
└── README.md                         # Instrucciones de ejecución
```

> **Nota de integración:** el frontend **ya consume el backend**. Los datos mock
> desaparecieron de `components/jobbi/data.ts`, que ahora solo describe la navegación;
> todo lo demás llega del API Gateway vía `lib/api.ts`. Las pantallas que agregan varios
> contextos usan las rutas `/api/bff/*`, de modo que el navegador hace una sola petición
> en lugar de conocer la topología interna.

> **Monetización** vive en `services/monetizacion/` (el simulador original se eliminó). El
> `Service` de Kubernetes conserva su nombre y puerto (`servicio-monetizacion:8000`), así
> que las pruebas k6 siguen siendo válidas. No expone ningún endpoint de cobro: la
> comisión se carga solo al consumir el evento.

## 🧪 Pruebas de Carga y Estrés (k6)

```bash
./scripts/run-load-test.sh [e2e|sns] [humo|carga|estres|pico] [vus]

./scripts/run-load-test.sh e2e carga        # flujo completo por el gateway
./scripts/run-load-test.sh e2e estres 60    # escalones hasta 60 VUs
./scripts/run-load-test.sh e2e pico         # ráfaga repentina
./scripts/run-load-test.sh sns carga        # solo el broker (SNS → SQS → worker)
```

En Windows: `powershell -ExecutionPolicy Bypass -File .\scripts\run-load-test.ps1 e2e carga`.
Usa k6 nativo si está instalado y, si no, la imagen `grafana/k6`.

- **`e2e`** necesita `kubectl port-forward svc/api-gateway 8080:8080 -n aws-local`.
- **`sns`** necesita los port-forwards de LocalStack (4566) y Monetización (8000).

Cada escenario de `e2e` termina verificando la **consistencia eventual**: la
comisión cobrada debe igualar la de los servicios completados, sin eventos
perdidos ni duplicados y con la DLQ vacía. Escenarios, métricas y resultados de
referencia en [docs/pubsub.md](docs/pubsub.md#e-pruebas-de-carga-estrés-y-pico-k6).

---

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

# Eliminar el API Gateway y los microservicios de dominio
kubectl delete -f k8s/gateway-deployment.yaml
kubectl delete -f k8s/domain-services.yaml

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
