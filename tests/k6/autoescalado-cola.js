// Carga por escalones sobre la cola de Monetización, para ver el auto-escalado.
//
// Publica directo en el tema SNS de LocalStack (como load-test-pubsub.js): el
// gateway se satura cerca de 20 TPS, mucho antes de poder llenar la cola, y lo
// que se quiere probar es el consumidor. Cada evento lleva un id de corrida
// (AUTO-<CORRIDA>-…) para contar al final que se procesaron todos, una sola vez.
// No tienen contratación real: Monetización los registra como
// REGISTRADO_SIN_CONTRATACION sin tocar billeteras.
//
// Lo corre scripts/caos/autoescalado-cola.sh dentro del clúster:
//   GUION=autoescalado-cola.js ./scripts/k6-en-cluster.sh autoescalado CORRIDA=x
//
// Escalones (eventos/s publicados en SNS; se suman a los ~2/s de la carga
// nominal de fondo). Un Pod con SQS_HILOS=1 y SQS_COSTO_MS=125 drena ~7/s:
//   BAJA   2/s  ×  1 min     → total ~4/s:  1 Pod va al día            → 1 réplica
//   MEDIA  8/s  ×  2,5 min   → total ~10/s: 1 Pod no alcanza, 2 sí     → 2 réplicas
//   ALTA  16/s  ×  3 min     → total ~18/s: 2 Pods no alcanzan, 3 sí   → 3 réplicas
// Se ajustan con -e TASA_BAJA / TASA_MEDIA / TASA_ALTA (y TRAMO_* en segundos).

import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const SNS_URL = __ENV.AWS_SNS_ENDPOINT || 'http://localstack.aws-local.svc.cluster.local:4566/';
const TOPIC_ARN = __ENV.TOPIC_ARN || 'arn:aws:sns:us-east-1:000000000000:contratacion-completada-topic';
const CORRIDA = __ENV.CORRIDA || `${Date.now()}`;

const tasa = (nombre, defecto) => parseInt(__ENV[nombre] || defecto, 10);
const BAJA = tasa('TASA_BAJA', 2);
const MEDIA = tasa('TASA_MEDIA', 8);
const ALTA = tasa('TASA_ALTA', 16);
const TRAMO_BAJA = tasa('TRAMO_BAJA', 60);
const TRAMO_MEDIA = tasa('TRAMO_MEDIA', 150);
const TRAMO_ALTA = tasa('TRAMO_ALTA', 180);

const escalon = (segundos, objetivo) => [
  { duration: '5s', target: objetivo },                     // cambio de escalón
  { duration: `${segundos - 5}s`, target: objetivo },
];

export const options = {
  scenarios: {
    escalones: {
      executor: 'ramping-arrival-rate',
      startRate: BAJA,
      timeUnit: '1s',
      preAllocatedVUs: 20,
      maxVUs: 60,
      stages: [
        ...escalon(TRAMO_BAJA, BAJA),
        ...escalon(TRAMO_MEDIA, MEDIA),
        ...escalon(TRAMO_ALTA, ALTA),
        { duration: '1s', target: 0 },
      ],
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    // La consistencia (publicados = procesados) la comprueba el script de caos.
  },
};

const publicados = new Counter('autoescalado_publicados');

export default function () {
  const evento = JSON.stringify({
    evento: 'CONTRATACION_COMPLETADA',
    monto: 1000,
    servicio_id: `AUTO-${CORRIDA}-${__VU}-${__ITER}`,
    timestamp: new Date().toISOString(),
  });
  const res = http.post(SNS_URL,
    `Action=Publish&TopicArn=${encodeURIComponent(TOPIC_ARN)}&Message=${encodeURIComponent(evento)}`,
    { headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, tags: { name: 'sns_publish' } });
  if (check(res, { 'publicado en SNS (200)': (r) => r.status === 200 })) {
    publicados.add(1);
  }
}
