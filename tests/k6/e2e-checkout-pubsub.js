// Prueba de punta a punta del Pub/Sub a través del API Gateway.
//
// Cada iteración es un servicio completo, igual que en la aplicación:
//
//   contactar → proponer tarifa → aceptar → check-in → check-out → calificar
//        └─ el check-out deja el evento en el outbox de Contrataciones
//           → relay → SNS → SQS → worker de Monetización → billetera
//
// Calificar cierra el servicio y su chat; la siguiente iteración abre un chat
// nuevo con el mismo prestador para acordar el siguiente servicio.
//
// En pico50 (o con -e BUSQUEDAS=0.5), la mitad de las veces el demandante
// vuelve a buscar antes del siguiente servicio y, con su propia probabilidad
// (20-70 %), contacta y contrata a otro prestador de los resultados. Mueve la
// tasa de contacto tras búsqueda (match rate) de Grafana.
//
// Qué mide, además de los tiempos HTTP:
//
//   pubsub_latencia_cobro_ms   check-out → comisión cobrada (en una muestra de
//                              iteraciones que sondean hasta ver COBRADO)
//   pubsub_cobro_a_tiempo      % de esa muestra cobrado en menos de 30 s
//   pubsub_servicios_cerrados  check-outs exitosos (eventos producidos)
//
// y al final (teardown) comprueba la **consistencia eventual**: por cada
// prestador, la comisión cobrada en Monetización debe igualar la de sus
// servicios completados en Contrataciones. Ni un evento perdido ni uno cobrado
// dos veces.
//
//   ./scripts/run-load-test.sh e2e carga
//   k6 run -e ESCENARIO=estres -e VUS=60 tests/k6/e2e-checkout-pubsub.js
//
// Necesita: kubectl port-forward svc/api-gateway 8080:8080 -n aws-local
// Crea datos propios (prestadores y demandantes "k6"): después de probar,
// ./scripts/reset-datos.sh deja el sistema limpio.

import http from 'k6/http';
import { check, fail, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { ESCENARIO, ESTRICTO, P95_CHECKOUT_MS, escenario } from './lib/escenarios.js';

const BASE = __ENV.BASE_URL || 'http://localhost:8080';
const PRESTADORES = parseInt(__ENV.PRESTADORES || '5', 10);
// Fracción de iteraciones que se quedan sondeando hasta ver el cobro. Sondear
// todas mediría bien la latencia pero restaría carga al productor.
const MUESTREO = parseFloat(__ENV.MUESTREO || (ESTRICTO ? '0.3' : '0.1'));
const ESPERA_COBRO_S = 30;
// Clientes que vuelven a buscar (tasa de contacto tras búsqueda, "match rate").
// Solo en pico50 por defecto: en nominal y en los fallos el flujo no cambia.
// BUSQUEDAS = probabilidad de que, antes de acordar el siguiente servicio, el
// demandante vuelva a buscar. Cada demandante decide contactar a alguien de los
// resultados con su propia probabilidad, al azar entre MATCH_MIN y MATCH_MAX.
const BUSQUEDAS = parseFloat(__ENV.BUSQUEDAS || (ESCENARIO === 'pico50' ? '0.5' : '0'));
const MATCH_MIN = parseFloat(__ENV.MATCH_MIN || '0.2');
const MATCH_MAX = parseFloat(__ENV.MATCH_MAX || '0.7');

const latenciaCobro = new Trend('pubsub_latencia_cobro_ms', true);
const cobroATiempo = new Rate('pubsub_cobro_a_tiempo');
const cerrados = new Counter('pubsub_servicios_cerrados');
const reemplazos = new Counter('prestadores_bloqueados_reemplazados');
const abandonados = new Counter('servicios_dejados_a_medias');
const noAprobados = new Counter('prestadores_no_aprobados');
const demandantesNoAprobados = new Counter('demandantes_no_aprobados');
const consistencia = new Rate('pubsub_consistencia_final');
const busquedas = new Counter('busquedas_realizadas');
const matchSimulado = new Rate('contacto_tras_busqueda');

export const options = {
  scenarios: escenario(),
  setupTimeout: '2m',
  teardownTimeout: '5m',
  thresholds: {
    http_req_failed: [ESTRICTO ? 'rate<0.01' : 'rate<0.05'],
    'http_req_duration{paso:checkout}': [`p(95)<${P95_CHECKOUT_MS}`],
    pubsub_latencia_cobro_ms: [ESTRICTO ? 'p(95)<5000' : 'p(95)<20000'],
    pubsub_cobro_a_tiempo: [ESTRICTO ? 'rate>0.99' : 'rate>0.90'],
    // Esto no se relaja en ningún escenario: bajo cualquier carga, lo cobrado
    // tiene que cuadrar con lo producido.
    pubsub_consistencia_final: ['rate==1'],
  },
};

const JSON_HEADERS = { 'Content-Type': 'application/json' };

// En proponer y aceptar, el 409 de un prestador bloqueado (RN-04) es una
// respuesta de negocio esperada, no un error HTTP: no cuenta en http_req_failed.
const ACEPTA_409 = http.expectedStatuses({ min: 200, max: 399 }, 409);

function post(ruta, cuerpo, paso) {
  const opciones = { headers: JSON_HEADERS, tags: { paso } };
  if (paso === 'proponer' || paso === 'aceptar') opciones.responseCallback = ACEPTA_409;
  return http.post(`${BASE}${ruta}`, JSON.stringify(cuerpo), opciones);
}

function get(ruta, paso) {
  return http.get(`${BASE}${ruta}`, { tags: { paso } });
}

function registrar(rol, run, n, extra = {}) {
  const correo = `k6.${rol.toLowerCase()}.${run}.${n}@carga.jobbi.co`;
  const res = post('/api/auth/registro', {
    nombreCompleto: `K6 ${rol} ${n}`,
    correo,
    telefono: '3001234567',
    numeroDocumento: `K${run}${n}`.slice(0, 30),
    rol,
    ...extra,
  }, 'registro');
  if (res.status === 201) return res.json();
  // Un reintento tras un fallo intermedio puede encontrar la cuenta ya creada.
  if (res.status === 409) return post('/api/auth/login', { correo }, 'registro').json();
  return null;
}

// Tras un fallo, la VU espera antes de reintentar, como haría una persona. Sin
// esta pausa, una VU que falla repite en bucle cerrado a cientos de peticiones
// por segundo y el informe mide ese bucle en vez del sistema.
function abandonar() {
  sleep(1);
}

// Si el servicio queda a medias (una petición se agotó a mitad del flujo), ese
// servicio sigue abierto y JOBBI no deja acordar otro con la misma persona
// ("el servicio anterior sigue abierto", 409). Una persona real no insistiría
// para siempre: se va y vuelve a empezar. La VU hace lo mismo: descarta su
// demandante y su contacto, y en la siguiente iteración se registra de nuevo.
// Sin esto, bajo saturación las VU quedan atascadas repitiendo el 409.
function dejarAMedias() {
  abandonados.add(1);
  yo = null;
  sleep(1);
}

// RN-04 bajo carga: tras ~10 servicios en efectivo la billetera del prestador
// llega al umbral y el gateway rechaza sus acuerdos nuevos. Como en el mercado
// real, el demandante sigue con otro prestador: la VU registra uno nuevo.
function bloqueado(res) {
  return res.status === 409 && String(res.body).includes('billetera bloqueada');
}

// El registro verifica al prestador con el aliado (RN-01): ~20 % sale RECHAZADO
// y, con el aliado caído, queda PENDIENTE. Solo un APROBADO puede acordar
// servicios, así que los demás se descartan, como en el mercado real.
function nuevoPrestador(data, etiqueta) {
  const sesion = registrar('Prestador', data.run, etiqueta, { tarifaReferencialBase: 80000 });
  if (!sesion) return null;
  if (sesion.perfilPrestador.estadoVerificacionActual !== 'APROBADA') {
    noAprobados.add(1);
    return null;
  }
  const id = sesion.perfilPrestador.id;
  post('/api/mercado/prestador-oficios',
    { prestadorId: id, oficioId: data.oficio, tarifaReferencial: 80000, anosExperiencia: 3 }, 'alta');
  return id;
}

function cambiarDePrestador(data) {
  const id = nuevoPrestador(data, `v${__VU}r${Date.now()}`);
  if (!id) return false;
  const chat = post('/api/bff/contactar', { demandanteId: yo.demandante, prestadorId: id }, 'contactar');
  if (chat.status !== 201) return false;
  reemplazos.add(1);
  yo = { ...yo, prestador: id, contacto: chat.json('contacto.id'), conversacion: chat.json('conversacion.id') };
  return true;
}

// El demandante vuelve a buscar y, según su propia probabilidad, contacta a un
// prestador de los resultados y lo contrata en esta iteración. El contacto cita
// la búsqueda (busquedaOrigenId): así sube la tasa de contacto tras búsqueda.
// Solo cuenta si el par demandante–prestador es nuevo (Mercado no duplica
// contactos), por eso elige a alguien distinto de su prestador actual.
function volverABuscar(data) {
  const res = post('/api/bff/busquedas', { demandanteId: yo.demandante }, 'buscar');
  if (res.status !== 201) return;
  busquedas.add(1);
  const quiereContactar = Math.random() < yo.probMatch;
  // Solo prestadores de carga (no las cuentas demo) que ofrecen el oficio de la prueba.
  const candidatos = (res.json('prestadores.items') || []).filter((p) =>
    p.id !== yo.prestador && String(p.nombreCompleto).startsWith('K6 ')
    && (p.ofertas || []).some((o) => o.oficioId === data.oficio));
  if (!quiereContactar || candidatos.length === 0) {
    matchSimulado.add(false);  // miró los resultados y siguió con su prestador de siempre
    return;
  }
  const elegido = candidatos[Math.floor(Math.random() * candidatos.length)];
  const chat = post('/api/bff/contactar', {
    demandanteId: yo.demandante, prestadorId: elegido.id, busquedaOrigenId: res.json('busqueda.id'),
  }, 'contactar');
  matchSimulado.add(chat.status === 201);
  if (chat.status !== 201) return;
  yo = { ...yo, prestador: elegido.id, contacto: chat.json('contacto.id'), conversacion: chat.json('conversacion.id') };
}

export function setup() {
  const estado = get('/api/bff/pubsub/estado', 'estado');
  if (estado.status !== 200) fail(`El gateway no responde en ${BASE}`);
  if (estado.json('modo') !== 'EVENTOS') fail('El gateway no está en modo EVENTOS (Pub/Sub)');

  const run = `${Date.now()}`;
  const oficio = post('/api/mercado/catalogo/oficio',
    { categoriaNombre: 'Hogar', oficioNombre: 'Plomería' }, 'alta').json('oficio.id');

  const prestadores = [];
  for (let i = 0; prestadores.length < PRESTADORES; i++) {
    if (i >= PRESTADORES * 5) fail(`Solo ${prestadores.length} de ${PRESTADORES} prestadores quedaron APROBADOS`);
    const id = nuevoPrestador({ run, oficio }, i);
    if (id) prestadores.push(id);
  }
  console.log(`Escenario '${ESCENARIO}' · ${PRESTADORES} prestadores · muestreo de latencia ${MUESTREO * 100}%`);
  return { run, oficio, prestadores };
}

// Estado por VU: cada VU es un demandante con su propio contacto, así dos VU
// nunca negocian sobre el mismo chat (una propuesta nueva reemplaza la abierta).
let yo = null;
// Cuántas veces esta VU tuvo que registrarse con otro documento porque el
// aliado rechazó el anterior.
let rechazosDemandante = 0;

// El demandante también se verifica al registrarse (RN-01) y solo un APROBADO
// contrata. Si el aliado lo rechaza, la VU vuelve con otra persona (otro
// documento); si quedó PENDIENTE (aliado caído, Fallo 1), repite la misma
// cuenta más tarde: el login vuelve a pedir la verificación.
function nuevoDemandante(data) {
  const sesion = registrar('Demandante', data.run, `v${__VU}d${rechazosDemandante}`);
  if (!sesion) return null;
  const estado = sesion.perfilDemandante.estadoVerificacionActual;
  if (estado === 'APROBADA') return sesion.perfilDemandante.id;
  demandantesNoAprobados.add(1);
  if (estado === 'RECHAZADA') rechazosDemandante += 1;
  return null;
}

export default function (data) {
  if (yo === null) {
    const prestador = data.prestadores[(__VU - 1) % data.prestadores.length];
    const demandante = nuevoDemandante(data);
    if (!demandante) return abandonar();
    const chat = post('/api/bff/contactar', { demandanteId: demandante, prestadorId: prestador }, 'contactar');
    if (chat.status !== 201) return abandonar();
    yo = { prestador, demandante, contacto: chat.json('contacto.id'), conversacion: chat.json('conversacion.id'),
           probMatch: MATCH_MIN + Math.random() * (MATCH_MAX - MATCH_MIN) };
  } else if (Math.random() < BUSQUEDAS) {
    volverABuscar(data);
  }

  const valor = 40000 + Math.floor(Math.random() * 16) * 5000;
  const propuesta = post('/api/bff/acuerdos', {
    contactoId: yo.contacto, conversacionId: yo.conversacion,
    demandanteId: yo.demandante, prestadorId: yo.prestador, oficioId: data.oficio,
    valorPropuesto: valor, medioPago: 'EFECTIVO', propuestoPor: 'DEMANDANTE',
  }, 'proponer');
  if (bloqueado(propuesta)) return cambiarDePrestador(data) || abandonar();
  if (!check(propuesta, { 'propuesta registrada': (r) => r.status === 201 })) return dejarAMedias();

  const aceptada = post(`/api/bff/acuerdos/${propuesta.json('acuerdo.id')}/aceptar`, { rol: 'PRESTADOR' }, 'aceptar');
  if (bloqueado(aceptada)) return cambiarDePrestador(data) || abandonar();
  if (!check(aceptada, { 'servicio creado': (r) => r.status === 200 && r.json('contratacion.id') })) return dejarAMedias();
  const id = aceptada.json('contratacion.id');

  const checkIn = post(`/api/bff/contrataciones/${id}/check-in`, {}, 'checkin');
  if (!check(checkIn, { 'check-in': (r) => r.status === 200 })) return dejarAMedias();

  const inicio = Date.now();
  const checkOut = post(`/api/bff/contrataciones/${id}/check-out`, {}, 'checkout');
  if (!check(checkOut, {
    'check-out completa el servicio': (r) => r.status === 200 && r.json('contratacion.estado') === 'COMPLETADA',
    'el cobro es asíncrono': (r) => r.json('cobroAsincrono') === true,
  })) return dejarAMedias();
  cerrados.add(1);

  if (Math.random() < MUESTREO) {
    let cobrado = false;
    while (Date.now() - inicio < ESPERA_COBRO_S * 1000) {
      if (get(`/api/bff/contrataciones/${id}/cobro`, 'seguimiento').json('etapa') === 'COBRADO') {
        cobrado = true;
        break;
      }
      sleep(0.2);
    }
    cobroATiempo.add(cobrado);
    if (cobrado) latenciaCobro.add(Date.now() - inicio);
  }

  // El demandante califica: el servicio queda cerrado y el chat, libre.
  const resena = post('/api/bff/resenas', {
    contratacionId: id, autorId: yo.demandante, receptorId: yo.prestador,
    puntuacion: 4 + Math.round(Math.random()), comentario: 'Prueba de carga',
  }, 'calificar');
  if (!check(resena, { 'servicio calificado y cerrado': (r) => r.status === 201 })) return dejarAMedias();

  // Calificar cierra también el chat de ese servicio: el siguiente se acuerda
  // en un chat nuevo, que es lo que devuelve "Contactar" ahora.
  const nuevo = post('/api/bff/contactar', { demandanteId: yo.demandante, prestadorId: yo.prestador }, 'contactar');
  if (!check(nuevo, { 'chat nuevo para el siguiente servicio': (r) => r.status === 201 && r.json('conversacion.id') !== yo.conversacion })) return dejarAMedias();
  yo.conversacion = nuevo.json('conversacion.id');

  sleep(Math.random() * 0.5);  // pausa entre servicios del mismo demandante
}

export function teardown(data) {
  console.log('Verificando consistencia eventual (comisión producida vs. cobrada)…');
  const pendientes = new Set(data.prestadores);
  const limite = Date.now() + 120 * 1000;
  const detalle = {};

  while (pendientes.size && Date.now() < limite) {
    for (const prestador of [...pendientes]) {
      const producida = get(`/api/contrataciones/resumen?prestadorId=${prestador}`, 'verificacion')
        .json('comisionTotalCompletada') || 0;
      const cobrada = get(`/api/monetizacion/resumen/prestador/${prestador}`, 'verificacion')
        .json('comisionesAcumuladas') || 0;
      detalle[prestador] = { producida, cobrada };
      if (Math.abs(producida - cobrada) < 1) pendientes.delete(prestador);
    }
    if (pendientes.size) sleep(2);
  }

  for (const [prestador, { producida, cobrada }] of Object.entries(detalle)) {
    const cuadra = Math.abs(producida - cobrada) < 1;
    consistencia.add(cuadra);
    console.log(`  ${cuadra ? '✓' : '✗'} prestador ${prestador.slice(0, 8)}: producida ${producida} · cobrada ${cobrada}`);
  }

  const estado = get('/api/bff/pubsub/estado', 'verificacion').json();
  const dlq = estado.consumidor && estado.consumidor.colas ? estado.consumidor.colas.dlq.visibles : 'n/d';
  consistencia.add(dlq === 0 || dlq === 'n/d');
  console.log(`  outbox pendiente: ${estado.productor.pendientes} · mensajes en DLQ: ${dlq}`);
  console.log(`  latencia outbox→SNS p95: ${estado.productor.latenciaPublicacionMs.p95} ms · ` +
              `check-out→cobro p95 (servidor): ${estado.consumidor.latenciaExtremoAExtremoMs.p95} ms`);
}
