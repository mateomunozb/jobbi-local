# Fallos inyectados y pruebas de carga (Punto 3 del Entregable 3)

Caso de uso perturbado: **registrar el pago en efectivo de una contratación
completada y aplicar la comisión pendiente sobre la billetera del prestador.**

Todo corre dentro de Minikube (costo cero). Antes de empezar:

```bash
docker update --memory 6g --memory-swap 6g --cpus 6 minikube   # capacidad para carga + monitoreo
./scripts/deploy-backend.sh && ./scripts/deploy-monitoring.sh
kubectl port-forward svc/grafana 3001:3000 -n monitoring        # tablero en vivo
```

Cada experimento deja su bitácora en `tests/caos/resultados/` (fotos de las
métricas antes/durante/después, eventos de Kubernetes, logs relevantes y la
verificación de integridad), y k6 su resumen en `tests/k6/resultados/`.

## 1. Pruebas de carga (punto 10.4)

k6 corre **como Job dentro del clúster** ([scripts/k6-en-cluster.sh](../scripts/k6-en-cluster.sh)):
desde el host, el `port-forward` se satura antes que el sistema y falsea el
punto de quiebre. Cada iteración es un servicio completo por el gateway
(proponer → aceptar con verificación RN-01 → check-in → check-out → cobro
asíncrono → calificar); **TPS = peticiones HTTP por segundo al gateway**
(~6 por iteración). La tasa de llegada es fija: si el sistema se atrasa, k6
lo delata con `dropped_iterations`.

| Perfil | Comando | Carga | Criterio de aceptación |
|---|---|---|---|
| Nominal | `./scripts/k6-en-cluster.sh nominal` | 12 TPS · 5 min | p95 check-out < 300 ms · errores < 1 % · consistencia = 100 % |
| Pico | `./scripts/k6-en-cluster.sh pico50` | 12 → **50 TPS** (2 min) → 12 | sin degradación financiera: consistencia = 100 %, DLQ = 0 |
| Estrés / quiebre | `./scripts/caos/estres-punto-de-quiebre.sh` | 75 → 125 → 250 → **350 TPS** → 12 | documentar el primer recurso saturado y la recuperación |
| Resistencia | `./scripts/k6-en-cluster.sh resistencia DURACION=2h` | 12 TPS · 1-2 h | memoria plana por Pod, pool y conexiones estables, 0 reinicios |

Humo previo (1 servicio): `./scripts/k6-en-cluster.sh humo`.

**Prestadores bloqueados bajo carga.** Con RN-04 activa, cada prestador de
prueba llega al umbral tras ~10 servicios y el gateway rechaza sus acuerdos
(409). La VU hace lo que haría el mercado: registra otro prestador y sigue
(`prestadores_bloqueados_reemplazados`). Ese 409 es una respuesta de negocio
esperada y no cuenta como error HTTP.

**Qué mirar en el quiebre.** El script toma una foto al final de cada
escalón. Se espera que se sature primero alguno de estos recursos:
- CPU de los Pods, limitada a 300 m (panel *CPU · % del límite*).
- Pool de SQLAlchemy (5 + 10 conexiones por servicio) frente a
  `max_connections = 100` de PostgreSQL, con 150 conexiones posibles (paneles
  *Pool de BD* y *PostgreSQL*; aparecen respuestas **503**).
- La tasa de llegada (`dropped_iterations` en el resumen de k6).

## 2. Los cuatro fallos (punto 11)

Todos siguen el mismo guion: carga nominal de fondo → foto ANTES → inyección →
foto DURANTE → restauración (con `trap`: se restaura aunque se interrumpa) →
foto DESPUÉS → **verificación de integridad**.

### Integridad del dinero

[scripts/caos/verificar-integridad.sh](../scripts/caos/verificar-integridad.sh)
revisa seis condiciones y todas deben dar 0: cobros dobles, pagos dobles,
billeteras con saldo descuadrado, comisiones perdidas (contratación completada
en efectivo sin cargo tras 120 s), eventos pendientes en los outbox y mensajes
en las dos DLQ. Es de solo lectura y se puede correr en cualquier momento.

### Fallo 1 · Red / dependencia externa

`./scripts/caos/fallo1-dependencia-externa.sh` (~7 min)

| | |
|---|---|
| Inyección | El aliado de verificación simulado ([verificacion_externa](../services/verificacion_externa/main.py)) responde con **8 000 ms** de retardo durante 2 min y luego con **504** durante 1,5 min (`POST /_caos`) |
| Patrones puestos a prueba | Adapter / ACL ([adaptador_verificacion.py](../services/confianza/adaptador_verificacion.py)) + **Circuit Breaker** ([resiliencia.py](../services/common/resiliencia.py)): timeout 2 s, se abre tras 3 fallos, semiabierto a los 30 s |
| Hipótesis | El aliado solo se consulta al **registrar** un prestador (y al renovar su veredicto cada 30 días); en la prueba, los prestadores nuevos que k6 registra al bloquearse los anteriores. El circuito se abre en segundos; los registros no se cuelgan y quedan **PENDIENTE** (no aparecen ni trabajan); acuerdos, check-out y cobro no cambian; al retirar el fallo el circuito se cierra solo y el reverificador de fondo resuelve a los PENDIENTE |
| Evidencia | Panel *Circuit Breaker* (CERRADO → ABIERTO → SEMIABIERTO → CERRADO), *Llamadas al aliado* (`rechazada_por_circuito`), *p95 aceptar acuerdo*; logs `circuit_breaker_cambio` en Confianza |
| Métrica de recuperación | Tiempo desde que se retira el fallo hasta que el circuito vuelve a CERRADO (≤ 30 s + 1 llamada) |

### Fallo 2 · Servicios: eliminación del Pod de Monetización

`./scripts/caos/fallo2-eliminar-pod-monetizacion.sh [--forzado]` (~6 min, 12 TPS)

| | |
|---|---|
| Inyección | `kubectl delete pod` del consumidor mientras cobra, dos veces; `--forzado` usa `--grace-period=0` |
| Patrones | Deployment (autorrecuperación) · Idempotent Receiver (`eventos_procesados`) · SQS al menos una vez + visibilidad 30 s |
| Hipótesis | El Pod de reemplazo queda listo en segundos; los mensajes en vuelo reaparecen y se cobran; los ya cobrados pero no confirmados llegan duplicados y se descartan |
| Evidencia | *Eventos consumidos* (aparece `DUPLICADO`), *Colas SQS* (sube y se drena), tiempo de reemplazo en la bitácora; integridad: **0 cobros dobles, 0 comisiones perdidas** |

### Fallo 3 · Base de datos: saturación temporal y auto-escalado en caliente

`./scripts/caos/fallo3-saturacion-bd.sh` (~11 min, 12 TPS de fondo) · tablero *6 · Fallo 3*

| | |
|---|---|
| Qué simula | El auto-escalado de **Aurora Serverless v2**, que LocalStack no emula. El servicio `escalador-bd` mide cada 5 s la CPU y la memoria de PostgreSQL en Prometheus y le cambia la capacidad **en caliente** con `pods/resize` de Kubernetes (sin reiniciar el Pod ni cortar conexiones). Es una simulación del mecanismo, no Aurora |
| Capacidad (ACU) | 0,5 ACU = 0,5 núcleos / 640 Mi (normal) → 1 ACU = 1 / 1 Gi → 2 ACU = 2 / 1,5 Gi → 4 ACU = 4 / 2 Gi. En cada nivel también sube `work_mem` (4 → 8 → 16 → 32 MB) con `ALTER SYSTEM`, para que la base use la memoria nueva |
| Reglas | **Sube** un nivel si la CPU usada pasa del 75 % de la asignada durante 10 s (o la memoria del 85 %), con 15 s entre subidas. **Baja** un nivel si lo usado cabría holgado (≤ 50 % de CPU, ≤ 80 % de memoria) en el nivel inferior durante 30 s |
| Inyección | Job `saturador-bd` con `pgbench`: 1 tps durante 150 s (~0,6 núcleos) y luego 4 tps durante 150 s (~2,4 núcleos). Cada transacción ordena ~200 000 filas generadas al vuelo, sin tocar tablas del negocio |
| Hipótesis | La capacidad sube en escalones mientras la base está saturada (0,5 → 1 → 2 → 4 ACU) y **vuelve sola a 0,5 ACU** al pasar la saturación; 0 reinicios de PostgreSQL; el check-out puede ponerse lento mientras la base está frenada y se recupera al subir la capacidad; integridad OK |
| Evidencia | *Capacidad (ACU)*, *CPU y memoria usadas vs. asignadas* (escalones punteados), *% de la CPU asignada* (línea de 75 %), *throttling*, marcas naranjas en cada escalado, historial de escalados y reinicios en la bitácora |

### Extra · Base de datos: caída total transitoria

`./scripts/caos/fallo3-caida-base-datos.sh [segundos=180]` (~8 min) · tablero *10 · Extra*

| | |
|---|---|
| Inyección | `kubectl scale statefulset/postgres --replicas=0` durante 180 s. Mientras está caída se publican **20 eventos** directamente en SNS, para asegurar que haya cobros esperando en la cola |
| Patrones | **Backoff exponencial** (1 → 30 s) en el relay del outbox y en los consumidores, que dejan de recibir mientras la base no responde · Transactional Outbox · 503 reintentable |
| Hallazgo previo (corregido) | Sin backoff, cada mensaje se recibía cada 30 s: tras 5 intentos (~150 s) **comisiones válidas terminaban en la DLQ**. Por eso la caída dura 180 s |
| Hipótesis | Endpoints en 503 sin reinicios de Pods; los 20 eventos esperan en SQS; tras la recuperación se cobran los 20, una sola vez cada uno; DLQ = 0 |
| Evidencia | *Errores 5xx* (503), *Colas SQS*, *Pool de BD*; logs `base_no_disponible` con `reintentoEnSegundos` creciente y `base_recuperada`; bitácora «publicados 20 · cobrados 20» |

### Fallo 4 · Recursos: OOMKilled

`./scripts/caos/fallo4-oomkilled.sh` (~7 min, 12 TPS)

| | |
|---|---|
| Inyección | Un proceso reserva 4 MiB por segundo dentro del contenedor de Monetización (`kubectl exec`), en el mismo cgroup, hasta el límite de **256 Mi** (~45 s). Luego la fuga se reinyecta en cada arranque hasta cumplir `CAIDA` (30 s por defecto) |
| Mecanismo | cgroup v2 con `memory.oom.group=1`: el kernel mata el contenedor entero → `lastState.terminated.reason = OOMKilled` → Kubernetes lo reinicia en el mismo Pod, con espera creciente (CrashLoopBackOff) si vuelve a morir |
| Hipótesis | El fallo queda contenido en ese Pod; los eventos se encolan en SQS mientras no hay consumidor y se cobran al volver; sin pérdidas ni dobles cobros; DLQ = 0 |
| Evidencia | *Memoria · % del límite* (rampa hasta 100 % y caída), *Cola de Monetización · visibles vs. en vuelo* (sube y se drena), *Flujo del cobro*, *Reinicios* (+3), *OOMKilled* = 1, alerta `ContenedorOOMKilled`; eventos `BackOff` de Kubernetes en la bitácora |

## 3. Plantilla de análisis (por experimento)

| Campo | Qué anotar |
|---|---|
| Inicio / fin de la inyección | de la bitácora |
| Impacto observado | p95 y % de 5xx durante el fallo vs. ANTES |
| Detección | ¿qué alerta o panel lo mostró primero? ¿a los cuántos segundos? |
| Recuperación (MTTR) | desde que se retira el fallo hasta que las métricas vuelven a la línea base |
| Integridad | resultado de `verificar-integridad.sh` (6 × 0) |
| Patrón que contuvo el fallo | Circuit Breaker / Idempotent Receiver / backoff / Deployment / outbox |
| Mejora propuesta | lo que el experimento dejó en evidencia |
