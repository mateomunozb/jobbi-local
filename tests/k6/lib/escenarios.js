// Perfiles de carga compartidos por las pruebas k6.
//
// Se elige con -e ESCENARIO=<nombre> y se escala con -e VUS=<máximo>:
//
//   humo    1 VU, pocas iteraciones: ¿funciona el camino completo?
//   carga   carga normal sostenida: ¿cumple los objetivos (thresholds)?
//   estres  escalones crecientes hasta VUS: ¿dónde empieza a degradarse?
//   pico    salto brusco a VUS y vuelta: ¿absorbe la ráfaga y se recupera?
//
// Perfiles por TPS del Entregable 3 (punto 10.4). Fijan la tasa de llegada, no
// los usuarios: si el sistema se atrasa, k6 no "espera" y lo delata con
// iteraciones descartadas (dropped_iterations). Se escalan con -e TPS=<n>.
//
//   nominal      ~12 TPS sostenidos (DURACION, 5m)        p95 checkout < 300 ms, errores < 1 %
//   pico50       12 → ráfaga de ~50 TPS → 12              procesa sin degradación financiera
//   quiebre      75 → 125 → 250 → 350 TPS y vuelta a 12   punto de quiebre y recuperación
//   resistencia  ~12 TPS durante DURACION (1h)             sin fugas de memoria ni conexiones
//
// TPS = peticiones HTTP por segundo al gateway. Cada iteración del flujo hace
// ~PETICIONES_POR_ITERACION peticiones, así que se lanzan TPS / 6 iteraciones/s.
//
// En estrés y pico los umbrales son más laxos a propósito: el objetivo no es
// "pasar" sino encontrar el límite y ver que el sistema no pierde eventos.

export const ESCENARIO = (__ENV.ESCENARIO || 'carga').toLowerCase();
const VUS = parseInt(__ENV.VUS || { humo: 1, carga: 10, estres: 60, pico: 80 }[ESCENARIO] || 10, 10);
const pct = (f) => Math.max(1, Math.round(VUS * f));

// proponer, aceptar, check-in, check-out, calificar y contactar (+ el sondeo
// ocasional del cobro).
export const PETICIONES_POR_ITERACION = 6;
const TPS = parseFloat(__ENV.TPS || { nominal: 12, pico50: 50, resistencia: 12 }[ESCENARIO] || 12);
// Iteraciones por minuto para un objetivo de TPS (la tasa de k6 es entera).
const porMinuto = (tps) => Math.max(1, Math.round((tps / PETICIONES_POR_ITERACION) * 60));
const DURACION = __ENV.DURACION || { nominal: '5m', resistencia: '1h' }[ESCENARIO] || '5m';
const escalon = (tps, subida, sostener) => [
  { duration: subida, target: porMinuto(tps) },
  { duration: sostener, target: porMinuto(tps) },
];

const PERFILES = {
  humo: {
    executor: 'shared-iterations',
    vus: 1,
    iterations: parseInt(__ENV.ITERACIONES || '3', 10),
    maxDuration: '2m',
  },
  carga: {
    executor: 'ramping-vus',
    startVUs: 0,
    stages: [
      { duration: '30s', target: VUS },  // rampa de subida
      { duration: '1m', target: VUS },   // carga sostenida
      { duration: '15s', target: 0 },    // enfriamiento
    ],
    gracefulRampDown: '30s',
  },
  estres: {
    executor: 'ramping-vus',
    startVUs: 0,
    stages: [
      { duration: '30s', target: pct(0.25) },
      { duration: '45s', target: pct(0.25) },
      { duration: '30s', target: pct(0.5) },
      { duration: '45s', target: pct(0.5) },
      { duration: '30s', target: pct(0.75) },
      { duration: '45s', target: pct(0.75) },
      { duration: '30s', target: VUS },
      { duration: '45s', target: VUS },
      { duration: '30s', target: 0 },    // ¿se recupera al bajar la carga?
    ],
    gracefulRampDown: '30s',
  },
  pico: {
    executor: 'ramping-vus',
    startVUs: 0,
    stages: [
      { duration: '15s', target: pct(0.1) },  // línea base
      { duration: '30s', target: pct(0.1) },
      { duration: '10s', target: VUS },       // ráfaga
      { duration: '40s', target: VUS },
      { duration: '10s', target: pct(0.1) },  // vuelta a la normalidad
      { duration: '40s', target: pct(0.1) },
      { duration: '10s', target: 0 },
    ],
    gracefulRampDown: '30s',
  },
  nominal: {
    executor: 'constant-arrival-rate',
    rate: porMinuto(TPS), timeUnit: '1m', duration: DURACION,
    preAllocatedVUs: 10, maxVUs: 60,
  },
  pico50: {
    executor: 'ramping-arrival-rate',
    startRate: porMinuto(12), timeUnit: '1m',
    preAllocatedVUs: 30, maxVUs: 200,
    stages: [
      ...escalon(12, '15s', '1m'),      // línea base nominal
      ...escalon(TPS, '10s', '2m'),     // ráfaga concentrada
      ...escalon(12, '10s', '1m'),      // ¿vuelve a la normalidad?
    ],
  },
  quiebre: {
    executor: 'ramping-arrival-rate',
    startRate: porMinuto(12), timeUnit: '1m',
    preAllocatedVUs: 100, maxVUs: 600,
    stages: [
      ...escalon(75, '30s', '2m'),      // 3x el pico de negocio
      ...escalon(125, '30s', '2m'),     // 5x
      ...escalon(250, '30s', '2m'),     // 10x
      ...escalon(350, '30s', '2m'),     // más allá, hasta romper
      ...escalon(12, '30s', '2m'),      // recuperación: ¿vuelve al nominal?
    ],
  },
  resistencia: {
    executor: 'constant-arrival-rate',
    rate: porMinuto(TPS), timeUnit: '1m', duration: DURACION,
    preAllocatedVUs: 10, maxVUs: 60,
  },
};

export function escenario() {
  const perfil = PERFILES[ESCENARIO];
  if (!perfil) {
    throw new Error(`ESCENARIO desconocido '${ESCENARIO}'. Usa: ${Object.keys(PERFILES).join(', ')}`);
  }
  return { [ESCENARIO]: perfil };
}

// true donde los umbrales son objetivos de servicio.
export const ESTRICTO = ['carga', 'humo', 'nominal', 'resistencia'].includes(ESCENARIO);
// SLO de latencia del check-out: 300 ms en nominal y resistencia (punto 10.4).
export const P95_CHECKOUT_MS = ['nominal', 'resistencia'].includes(ESCENARIO) ? 300 : (ESTRICTO ? 1000 : 3000);
