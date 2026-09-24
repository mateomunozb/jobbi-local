// Perfiles de carga compartidos por las pruebas k6.
//
// Se elige con -e ESCENARIO=<nombre> y se escala con -e VUS=<máximo>:
//
//   humo    1 VU, pocas iteraciones: ¿funciona el camino completo?
//   carga   carga normal sostenida: ¿cumple los objetivos (thresholds)?
//   estres  escalones crecientes hasta VUS: ¿dónde empieza a degradarse?
//   pico    salto brusco a VUS y vuelta: ¿absorbe la ráfaga y se recupera?
//
// En estrés y pico los umbrales son más laxos a propósito: el objetivo no es
// "pasar" sino encontrar el límite y ver que el sistema no pierde eventos.

export const ESCENARIO = (__ENV.ESCENARIO || 'carga').toLowerCase();
const VUS = parseInt(__ENV.VUS || { humo: 1, carga: 10, estres: 60, pico: 80 }[ESCENARIO] || 10, 10);
const pct = (f) => Math.max(1, Math.round(VUS * f));

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
};

export function escenario() {
  const perfil = PERFILES[ESCENARIO];
  if (!perfil) {
    throw new Error(`ESCENARIO desconocido '${ESCENARIO}'. Usa: ${Object.keys(PERFILES).join(', ')}`);
  }
  return { [ESCENARIO]: perfil };
}

// true en carga y humo, donde los umbrales son objetivos de servicio.
export const ESTRICTO = ESCENARIO === 'carga' || ESCENARIO === 'humo';
