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
import { ESCENARIO, ESTRICTO, escenario } from './lib/escenarios.js';

const BASE = __ENV.BASE_URL || 'http://localhost:8080';
const PRESTADORES = parseInt(__ENV.PRESTADORES || '5', 10);
// Fracción de iteraciones que se quedan sondeando hasta ver el cobro. Sondear
// todas mediría bien la latencia pero restaría carga al productor.
const MUESTREO = parseFloat(__ENV.MUESTREO || (ESTRICTO ? '0.3' : '0.1'));
const ESPERA_COBRO_S = 30;

const latenciaCobro = new Trend('pubsub_latencia_cobro_ms', true);
const cobroATiempo = new Rate('pubsub_cobro_a_tiempo');
const cerrados = new Counter('pubsub_servicios_cerrados');
const consistencia = new Rate('pubsub_consistencia_final');

export const options = {
  scenarios: escenario(),
  setupTimeout: '2m',
  teardownTimeout: '5m',
  thresholds: {
    http_req_failed: [ESTRICTO ? 'rate<0.01' : 'rate<0.05'],
    'http_req_duration{paso:checkout}': [ESTRICTO ? 'p(95)<1000' : 'p(95)<3000'],
    pubsub_latencia_cobro_ms: [ESTRICTO ? 'p(95)<5000' : 'p(95)<20000'],
    pubsub_cobro_a_tiempo: [ESTRICTO ? 'rate>0.99' : 'rate>0.90'],
    // Esto no se relaja en ningún escenario: bajo cualquier carga, lo cobrado
    // tiene que cuadrar con lo producido.
    pubsub_consistencia_final: ['rate==1'],
  },
};

const JSON_HEADERS = { 'Content-Type': 'application/json' };

function post(ruta, cuerpo, paso) {
  return http.post(`${BASE}${ruta}`, JSON.stringify(cuerpo), { headers: JSON_HEADERS, tags: { paso } });
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

export function setup() {
  const estado = get('/api/bff/pubsub/estado', 'estado');
  if (estado.status !== 200) fail(`El gateway no responde en ${BASE}`);
  if (estado.json('modo') !== 'EVENTOS') fail('El gateway no está en modo EVENTOS (Pub/Sub)');

  const run = `${Date.now()}`;
  const oficio = post('/api/mercado/catalogo/oficio',
    { categoriaNombre: 'Hogar', oficioNombre: 'Plomería' }, 'alta').json('oficio.id');

  const prestadores = [];
  for (let i = 0; i < PRESTADORES; i++) {
    const sesion = registrar('Prestador', run, i, { tarifaReferencialBase: 80000 });
    if (!sesion) fail(`No se pudo registrar el prestador ${i}`);
    const id = sesion.perfilPrestador.id;
    post('/api/mercado/prestador-oficios',
      { prestadorId: id, oficioId: oficio, tarifaReferencial: 80000, anosExperiencia: 3 }, 'alta');
    prestadores.push(id);
  }
  console.log(`Escenario '${ESCENARIO}' · ${PRESTADORES} prestadores · muestreo de latencia ${MUESTREO * 100}%`);
  return { run, oficio, prestadores };
}

// Estado por VU: cada VU es un demandante con su propio contacto, así dos VU
// nunca negocian sobre el mismo chat (una propuesta nueva reemplaza la abierta).
let yo = null;

export default function (data) {
  if (yo === null) {
    const prestador = data.prestadores[(__VU - 1) % data.prestadores.length];
    const sesion = registrar('Demandante', data.run, `v${__VU}`);
    if (!sesion) return abandonar();
    const demandante = sesion.perfilDemandante.id;
    const chat = post('/api/bff/contactar', { demandanteId: demandante, prestadorId: prestador }, 'contactar');
    if (chat.status !== 201) return abandonar();
    yo = { prestador, demandante, contacto: chat.json('contacto.id'), conversacion: chat.json('conversacion.id') };
  }

  const valor = 40000 + Math.floor(Math.random() * 16) * 5000;
  const propuesta = post('/api/bff/acuerdos', {
    contactoId: yo.contacto, conversacionId: yo.conversacion,
    demandanteId: yo.demandante, prestadorId: yo.prestador, oficioId: data.oficio,
    valorPropuesto: valor, medioPago: 'EFECTIVO', propuestoPor: 'DEMANDANTE',
  }, 'proponer');
  if (!check(propuesta, { 'propuesta registrada': (r) => r.status === 201 })) return abandonar();

  const aceptada = post(`/api/bff/acuerdos/${propuesta.json('acuerdo.id')}/aceptar`, { rol: 'PRESTADOR' }, 'aceptar');
  if (!check(aceptada, { 'servicio creado': (r) => r.status === 200 && r.json('contratacion.id') })) return abandonar();
  const id = aceptada.json('contratacion.id');

  const checkIn = post(`/api/bff/contrataciones/${id}/check-in`, {}, 'checkin');
  if (!check(checkIn, { 'check-in': (r) => r.status === 200 })) return abandonar();

  const inicio = Date.now();
  const checkOut = post(`/api/bff/contrataciones/${id}/check-out`, {}, 'checkout');
  if (!check(checkOut, {
    'check-out completa el servicio': (r) => r.status === 200 && r.json('contratacion.estado') === 'COMPLETADA',
    'el cobro es asíncrono': (r) => r.json('cobroAsincrono') === true,
  })) return abandonar();
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
  if (!check(resena, { 'servicio calificado y cerrado': (r) => r.status === 201 })) return abandonar();

  // Calificar cierra también el chat de ese servicio: el siguiente se acuerda
  // en un chat nuevo, que es lo que devuelve "Contactar" ahora.
  const nuevo = post('/api/bff/contactar', { demandanteId: yo.demandante, prestadorId: yo.prestador }, 'contactar');
  if (!check(nuevo, { 'chat nuevo para el siguiente servicio': (r) => r.status === 201 && r.json('conversacion.id') !== yo.conversacion })) return abandonar();
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
