// Prueba del broker: publica directo en el tema SNS de LocalStack.
//
// Mide solo la infraestructura de mensajería (SNS → SQS → worker), sin pasar
// por el gateway ni por las bases de los demás contextos. Los eventos no tienen
// contratación real, así que Monetización los registra como
// REGISTRADO_SIN_CONTRATACION sin tocar ninguna billetera.
//
//   k6 run -e ESCENARIO=carga tests/k6/load-test-pubsub.js
//   (o ./scripts/run-load-test.sh sns carga)
//
// Necesita: kubectl port-forward svc/localstack 4566:4566 -n aws-local
//           kubectl port-forward svc/servicio-monetizacion 8000:8000 -n aws-local

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';
import { ESTRICTO, escenario } from './lib/escenarios.js';

export const options = {
  scenarios: escenario(),
  thresholds: {
    http_req_failed: [ESTRICTO ? 'rate<0.01' : 'rate<0.05'],
    http_req_duration: [ESTRICTO ? 'p(95)<500' : 'p(95)<2000'],
  },
};

const LOCALSTACK_SNS_URL = __ENV.AWS_SNS_ENDPOINT || 'http://localhost:4566/';
const MONETIZACION_URL = __ENV.MONETIZACION_URL || 'http://localhost:8000';
const TOPIC_ARN = __ENV.TOPIC_ARN || 'arn:aws:sns:us-east-1:000000000000:contratacion-completada-topic';

const publicados = new Counter('pubsub_eventos_publicados');

// Cuántos eventos había procesado el worker antes de empezar, para comparar
// al final solo lo que publicó esta ejecución.
export function setup() {
  const res = http.get(`${MONETIZACION_URL}/cobros?limite=1`);
  return { procesadosAntes: res.status === 200 ? res.json('total') : null };
}

export default function () {
  const payload = JSON.stringify({
    evento: 'CONTRATACION_COMPLETADA',
    monto: Math.floor(Math.random() * 50000) + 10000,
    servicio_id: `SERV-K6-${__VU}-${__ITER}`,
    usuario_id: `USER-${__VU}`,
    timestamp: new Date().toISOString(),
  });

  // Query API de SNS (la que usan los SDK por debajo).
  const body = `Action=Publish&TopicArn=${encodeURIComponent(TOPIC_ARN)}&Message=${encodeURIComponent(payload)}`;
  const res = http.post(LOCALSTACK_SNS_URL, body, {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    tags: { name: 'sns_publish' },
  });

  if (check(res, { 'Publicación en SNS exitosa (HTTP 200)': (r) => r.status === 200 })) {
    publicados.add(1);
  }
  sleep(0.2);
}

// Consistencia eventual: espera a que el worker haya procesado todo lo
// publicado. Lo publicado se lee del resumen de k6, así que aquí solo se
// informa el total procesado; run-load-test.sh compara ambos números.
export function teardown(data) {
  if (data.procesadosAntes === null) {
    console.warn(`No se pudo consultar ${MONETIZACION_URL}: ¿está el port-forward de Monetización?`);
    return;
  }
  let anterior = -1;
  for (let i = 0; i < 60; i++) {
    const total = http.get(`${MONETIZACION_URL}/cobros?limite=1`).json('total');
    if (total === anterior) break;  // dejó de crecer: la cola se drenó
    anterior = total;
    sleep(2);
  }
  console.log(`Eventos procesados por el worker en esta ejecución: ${anterior - data.procesadosAntes}`);
}
