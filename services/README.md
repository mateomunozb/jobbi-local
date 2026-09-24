# Backend JOBBI — Microservicios por contexto delimitado

Backend derivado del modelo de dominio de JOBBI. Cada contexto delimitado del
diagrama de clases es un microservicio con su propio proceso, su propio puerto y
su propio `Service` de Kubernetes. Un **API Gateway** es el único punto de
entrada, y es a quien consume el frontend.

**La base es la única fuente de verdad: no hay datos sembrados.** Las nueve
bases arrancan vacías y todo lo que existe llega por el uso real de la
aplicación, incluido el catálogo de categorías y oficios, que se construye desde
el registro de cada prestador.

---

## 1. Mapa de contextos

| Servicio | Contexto delimitado | Puerto | Entidades que posee |
|---|---|---|---|
| `identidad` | Identidad y Perfiles | 8001 | Usuario, PerfilDemandante, PerfilPrestador, Ubicación |
| `mercado` | Mercado de Oficios | 8002 | Categoría, Oficio, PrestadorOficio, Búsqueda, Contacto |
| `contrataciones` | Contrataciones | 8003 | Contratación (ciclo de vida y comisión) |
| `comunicacion` | Comunicación | 8004 | Conversación, Mensaje, Notificación |
| `confianza` | Confianza y Verificación | 8005 | VerificaciónIdentidad, AliadoVerificación, Reseña |
| `monetizacion` | Monetización | 8000 | Pago, SuscripciónPro, BilleteraPrestador, MovimientoBilletera |
| `soporte` | Soporte y Disputas | 8006 | Incidente |
| `adquisicion` | Adquisición y Distribución | 8007 | AliadoDistribución, Referido |
| `proteccion` | Protección / Seguros | 8008 | Aseguradora, PlanProtección *(reservado, fuera de Fase 1)* |
| `gateway` | API Gateway | 8080 | — (enruta y compone) |

### Cómo se respeta la frontera

- **Una base de datos por contexto.** `jobbi_identidad`, `jobbi_mercado`, … en
  una única instancia de PostgreSQL. No es convención: el motor no permite
  consultar entre bases distintas, así que un JOIN entre contextos no es
  expresable. Cada Deployment recibe solo su cadena de conexión.
- **Las referencias cruzadas son solo UUID.** `Contratación` conoce
  `prestadorId`, no el nombre ni el correo del prestador: eso vive en
  `identidad`. Por eso las columnas que apuntan a otro contexto son `String`
  sin `ForeignKey`: la integridad entre contextos la sostiene el flujo de la
  aplicación, no el motor. Los UUID de la siembra se declaran una sola vez en
  [common/ids.py](common/ids.py) para que las referencias sean consistentes.
- **Quien cruza la frontera es el gateway**, y solo componiendo respuestas: pide
  cada dato al servicio dueño y los junta. Si un contexto no responde, marca esa
  parte como degradada en vez de tumbar la respuesta completa.
- **Vocabulario compartido, datos no.** Las enumeraciones del modelo
  ([common/enums.py](common/enums.py)) son lenguaje ubicuo y sí se comparten.

### Una imagen, nueve despliegues

Los diez procesos corren la misma imagen `jobbi/backend:v1`; cada `Deployment`
arranca un módulo distinto vía `SERVICE_MODULE`. Reduce el tiempo de build y
garantiza que todos los contextos usen las mismas dependencias, sin debilitar la
frontera: cada contexto sigue siendo su propio Pod, su propio Service y su
propio ciclo de vida.

---

## 2. Endpoints

Todo se consume por el gateway con el prefijo `/api/{servicio}`. Cada servicio
publica además su Swagger en `/docs`.

### Operación (gateway)

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Salud del gateway |
| GET | `/health/servicios` | Estado agregado de los 9 contextos |
| GET | `/api/servicios` | Catálogo de contextos enrutados |

### Autenticación — `/api/auth`

Los **únicos** endpoints que escriben. La verificación de identidad se da por
aprobada siempre y el login solo comprueba que el correo exista: es el alcance
acordado para esta fase, documentado en `identidad/auth.py`.

| Método | Ruta | Cuerpo | Respuestas |
|---|---|---|---|
| POST | `/auth/registro` | `nombreCompleto`, `correo`, `telefono`, `tipoDocumento`, `numeroDocumento`, `rol`; opcionales `municipio`, `barrio`, `descripcion`, `tarifaReferencialBase` | `201` · `409` correo repetido · `422` datos inválidos |
| POST | `/auth/login` | `correo` | `200` · `404` correo inexistente |

### Otras escrituras

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/api/mercado/catalogo/oficio` | Registra un oficio y crea su categoría si falta |
| POST | `/api/mercado/prestador-oficios` | Asocia un oficio a un prestador (idempotente: actualiza) |
| POST | `/api/mercado/contactos` | Abre el contacto demandante ↔ prestador (idempotente) |
| POST | `/api/comunicacion/conversaciones` | Abre la conversación de un contacto (idempotente) |
| POST | `/api/comunicacion/mensajes` | Envía un mensaje |
| POST | `/api/comunicacion/notificaciones` | Registra una notificación |
| POST | `/api/soporte/incidentes` | Reporta un incidente |
| POST | `/api/bff/contactar` | Contacto + conversación en una llamada |

Ambos devuelven `SesionResponse`: `usuario`, `rol`, `perfilDemandante`,
`perfilPrestador` y `verificado`. El rol se deduce de qué perfil tenga el
usuario, y es lo que el frontend usa para elegir la pantalla de destino.

### Identidad y Perfiles — `/api/identidad`

| Método | Ruta | Filtros |
|---|---|---|
| GET | `/usuarios` | `estado`, `q`, `page`, `size` |
| GET | `/usuarios/{id}` | |
| GET | `/usuarios/{id}/perfiles` | perfiles activados por el usuario |
| GET | `/demandantes` | `municipio` |
| GET | `/demandantes/{id}` | |
| GET | `/demandantes/por-usuario/{usuarioId}` | |
| GET | `/prestadores` | `verificado`, `plan`, `estadoVerificacion`, `calificacionMinima`, `municipio`, `q`, `orden` |
| GET | `/prestadores/{id}` | |
| GET | `/prestadores/por-usuario/{usuarioId}` | |
| GET | `/enums` | EstadoCuenta, EstadoVerificacion, PlanPrestador |

### Mercado de Oficios — `/api/mercado`

| Método | Ruta | Filtros |
|---|---|---|
| GET | `/categorias` · `/categorias/{id}` · `/categorias/{id}/oficios` | `q` |
| GET | `/oficios` · `/oficios/{id}` | `categoriaId`, `q` |
| GET | `/oficios/{id}/prestadores` | `experienciaMinima` |
| GET | `/prestador-oficios` | `prestadorId`, `oficioId`, `tarifaMaxima` |
| GET | `/prestadores/{id}/oficios` | oferta con detalle del oficio y categoría |
| GET | `/busquedas` · `/busquedas/{id}` | `demandanteId`, `categoriaId`, `municipio` |
| GET | `/contactos` · `/contactos/{id}` | `demandanteId`, `prestadorId`, `estado`, `busquedaOrigenId` |
| GET | `/resumen/catalogo` | conteo de oficios y ofertas por categoría |

### Contrataciones — `/api/contrataciones`

| Método | Ruta | Filtros |
|---|---|---|
| GET | `/contrataciones` | `demandanteId`, `prestadorId`, `oficioId`, `estado`, `medioPago`, `desde`, `hasta`, `valorMinimo` |
| GET | `/contrataciones/{id}` | |
| GET | `/contrataciones/{id}/timeline` | pasos alcanzados y duración real del servicio |
| GET | `/resumen` | `prestadorId`, `demandanteId` — totales, comisión y ticket promedio |
| GET | `/enums` | EstadoContratacion, MedioPago, flujo canónico |
| GET | `/outbox` | `estado`, `agregadoId`, `tipo` — eventos de la bandeja de salida |
| GET | `/outbox/resumen` | pendientes, publicados, latencia outbox → SNS y estado del relay |
| POST | `/outbox/publicar` | despierta el relay (p. ej. tras recuperar LocalStack) |

El check-out (`POST /contrataciones/{id}/check-out`) guarda el evento
`CONTRATACION_COMPLETADA` en `outbox_eventos` en la misma transacción, y el relay
lo publica en SNS. Ver [docs/pubsub.md](../docs/pubsub.md).

### Comunicación — `/api/comunicacion`

| Método | Ruta | Filtros |
|---|---|---|
| GET | `/conversaciones` | `contactoId`, `participanteId` — incluye no leídos y último mensaje |
| GET | `/conversaciones/{id}` · `/conversaciones/{id}/mensajes` | `leido` |
| GET | `/mensajes/{id}` | |
| GET | `/notificaciones` · `/notificaciones/{id}` | `usuarioId`, `leida`, `tipo`, `canal` |
| GET | `/notificaciones/resumen` | `usuarioId` — no leídas por canal |

### Confianza y Verificación — `/api/confianza`

| Método | Ruta | Filtros |
|---|---|---|
| GET | `/aliados-verificacion` · `/aliados-verificacion/{id}` | `slaMaximoHoras` |
| GET | `/verificaciones` · `/verificaciones/{id}` | `prestadorId`, `aliadoVerificacionId`, `estado`, `tipoVerificacion` |
| GET | `/prestadores/{id}/estado-verificacion` | estado consolidado e insignia vigente |
| GET | `/resenas` · `/resenas/{id}` | `receptorId`, `autorId`, `contratacionId`, `estadoModeracion`, `puntuacionMinima` |
| GET | `/resenas/resumen` | `receptorId`, `soloAprobadas` — promedio y distribución |
| GET | `/moderacion/cola` | reseñas pendientes de moderar |
| GET | `/enums` | EstadoVerificacion, EstadoModeracion |

### Monetización — `/api/monetizacion`

| Método | Ruta | Filtros |
|---|---|---|
| GET | `/pagos` · `/pagos/{id}` | `contratacionId`, `estado`, `desde`, `hasta` |
| GET | `/suscripciones` · `/suscripciones/{id}` | `prestadorId`, `estado` |
| GET | `/billeteras` · `/billeteras/{id}` | `prestadorId`, `bloqueada`, `saldoMinimo` |
| GET | `/billeteras/{id}/movimientos` | `tipo`, `desde`, `hasta` |
| GET | `/movimientos` | `billeteraId`, `contratacionId`, `tipo` |
| GET | `/resumen/prestador/{id}` | billetera, suscripción activa y comisiones |
| GET | `/planes` | tarifa de comisión de cada plan |
| GET | `/cobros` | `limite` — últimos eventos procesados por el worker SQS (persistidos) |
| GET | `/cobros/estado-worker` | worker, profundidad de cola y DLQ, resultados y latencia check-out → cobro |
| GET | `/cobros/contratacion/{id}` | si la comisión de esa contratación ya se cobró |
| POST | `/comisiones` | cobro síncrono (solo sin Pub/Sub; misma regla idempotente que el worker) |
| POST | `/cobrar` | cobro síncrono *(Pub/Sub original)* |

### Soporte, Adquisición y Protección

| Método | Ruta | Filtros |
|---|---|---|
| GET | `/api/soporte/incidentes` · `/{id}` | `contratacionId`, `estado`, `tipo`, `abiertos` |
| GET | `/api/soporte/incidentes/resumen` | conteos y días promedio de cierre |
| GET | `/api/adquisicion/aliados-distribucion` · `/{id}` · `/{id}/referidos` | `tipo`, `zona`, `activado` |
| GET | `/api/adquisicion/referidos` · `/resumen/canal` | tasa de activación por aliado |
| GET | `/api/proteccion/aseguradoras` · `/{id}` | |
| GET | `/api/proteccion/planes-proteccion` · `/{id}` | `contratacionId`, `aseguradoraId`, `estado` |

### Ciclo de vida de una contratación

Terminar el trabajo (check-out) no cierra el servicio: todavía se puede calificar
(una vez por parte) y reportar un incidente (uno). **Cuando el demandante lo
califica, el servicio queda cerrado**: no admite más calificaciones ni reportes, y
el chat queda libre para acordar un servicio nuevo. Mientras el anterior no esté
cerrado no se puede acordar otro con la misma persona. El gateway calcula estas
reglas en un solo lugar (`ciclo` en `/api/bff/contrataciones/{id}`) y el proxy
genérico rechaza `POST /api/confianza/resenas` y `POST /api/soporte/incidentes`
para que nadie se las salte.

### Chat por servicio y en tiempo real

- **Un chat por servicio.** `POST /api/bff/contactar` devuelve el chat ABIERTO
  con esa persona, o abre uno nuevo si todos están cerrados. Cuando el
  demandante califica el servicio, el gateway cierra su chat
  (`POST /conversaciones/{id}/cerrar` en Comunicación, que el proxy genérico no
  expone) y queda de solo lectura.
- **WebSocket:** `ws://…/ws/comunicacion/conversaciones/{id}?usuarioId=…`
  (gateway → Comunicación). El cliente envía `{"tipo":"mensaje","contenido":…}`
  y `{"tipo":"escribiendo"}`; recibe `conectado`, `presencia`, `mensaje`,
  `escribiendo`, `cerrada` y `error`. Un mensaje enviado por HTTP también se
  difunde. Una conversación inexistente cierra con código 4404.
- **Columnas nuevas en bases existentes:** `common/db.completar_columnas` añade
  al arrancar las columnas que falten (nunca cambia ni borra nada).

### Composición BFF — `/api/bff`

Agregan varios contextos en una sola llamada, para que el frontend no tenga que
conocer la topología interna ni encadenar seis peticiones.

| Ruta | Compone |
|---|---|
| `/api/bff/prestadores/{id}` | identidad + mercado + confianza + contrataciones + monetización + adquisición |
| `/api/bff/contrataciones/{id}` | contrataciones + identidad + mercado + monetización + confianza + soporte + protección |
| `/api/bff/demandantes/{id}/inicio` | identidad + mercado + contrataciones + comunicación |
| `/api/bff/catalogo` | mercado + identidad (pantalla de búsqueda) |
| `/api/bff/mensajes` | mercado + comunicación + identidad + contrataciones (bandeja: acuerdo vigente, `ciclo` y `servicioAnterior`); base de *Solicitudes* del prestador |
| `/api/bff/contactar` *(POST)* | mercado + comunicación (abre contacto y conversación) y avisa al prestador (`NUEVO_CONTACTO`) |
| `/api/bff/admin/metricas` | tablero del rol Admin: 6 contextos |
| `/api/bff/resenas` *(POST)* | contrataciones + confianza + identidad: califica una sola vez y solo un servicio terminado |
| `/api/bff/incidentes` *(POST)* | contrataciones + confianza + soporte: un reporte por servicio, mientras no esté cerrado |
| `/api/bff/contrataciones/{id}/cobro` | contrataciones (outbox) + monetización: etapa `EN_OUTBOX → PUBLICADO → COBRADO` |
| `/api/bff/pubsub/estado` | tablero del Pub/Sub: outbox, relay, colas, DLQ y worker |

---

## 3. Lecturas y escrituras

Por diseño, esta fase expone **operaciones de lectura** salvo tres excepciones:

- `POST /auth/registro` y `POST /auth/login`, que sustentan el acceso a la app.
- `POST /cobrar`, que ya existía en el simulador de Monetización y se conserva
  para no romper el flujo Pub/Sub ni la prueba de carga k6.

Todo lo demás es `GET`.

Todos los listados devuelven la misma envoltura:

```json
{ "total": 4, "page": 1, "size": 20, "pages": 1, "items": [ ... ] }
```

Un id inexistente devuelve `404`; un valor inválido de enumeración, `422`.

---

## 4. Estado inicial

Las nueve bases arrancan **vacías**. Cada servicio crea sus tablas al arrancar y
no escribe ni una fila: no hay usuarios, contrataciones, reseñas ni catálogo
definidos en el código.

Fuera de Kubernetes, sin `DATABASE_URL`, cada contexto usa su propio archivo
SQLite en `.datos/` — un archivo por contexto, misma separación.

### Cómo se llena el sistema

| Acción en la app | Qué se crea, y en qué contexto |
|---|---|
| Registrar un prestador | `Usuario` + `PerfilPrestador` (Identidad), `Categoria` + `Oficio` + `PrestadorOficio` (Mercado) |
| Registrar un demandante | `Usuario` + `PerfilDemandante` (Identidad) |
| Pulsar "Contactar" | `Contacto` (Mercado) + `Conversacion` (Comunicación) |
| Escribir en el chat | `Mensaje` (Comunicación) |
| Reportar un incidente | `Incidente` (Soporte) |

Contrataciones, Confianza, Monetización, Adquisición y Protección siguen siendo
de solo lectura: sus tablas permanecen vacías hasta que se implementen sus
flujos de escritura.

### Estructura de cada contexto

| Archivo | Papel |
|---|---|
| `models.py` | Esquemas de respuesta (Pydantic) — el contrato de la API |
| `tablas.py` | Tablas (SQLAlchemy) — cómo se guardan |
| `main.py` | Endpoints; traducen los filtros a cláusulas `WHERE` |

## 5. Pruebas

```bash
../.venv/bin/pip install -r requirements-dev.txt
../.venv/bin/python -m pytest -v      # desde services/
```
