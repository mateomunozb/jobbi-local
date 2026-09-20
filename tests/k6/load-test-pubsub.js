import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '10s', target: 5 },   // Rampa de subida a 5 usuarios concurrentes
    { duration: '30s', target: 20 },  // Carga sostenida con 20 VUs publicando eventos
    { duration: '10s', target: 0 },   // Rampa de enfriamiento
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],    // Tolerancia a fallos menor al 1%
    http_req_duration: ['p(95)<500'],  // 95% de las peticiones en menos de 500ms
  },
};

const LOCALSTACK_SNS_URL = __ENV.AWS_SNS_ENDPOINT || 'http://localhost:4566/';
const TOPIC_ARN = __ENV.TOPIC_ARN || 'arn:aws:sns:us-east-1:000000000000:contratacion-completada-topic';

export default function () {
  const payload = JSON.stringify({
    evento: 'CONTRATACION_COMPLETADA',
    monto: Math.floor(Math.random() * 50000) + 10000,
    servicio_id: `SERV-K6-${__VU}-${__ITER}`,
    usuario_id: `USER-${__VU}`,
    timestamp: new Date().toISOString()
  });

  const params = {
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
    },
  };

  // Petición Query API de SNS para publicar eventos
  const body = `Action=Publish&TopicArn=${encodeURIComponent(TOPIC_ARN)}&Message=${encodeURIComponent(payload)}`;

  const res = http.post(LOCALSTACK_SNS_URL, body, params);

  check(res, {
    'Publicación en SNS exitosa (HTTP 200)': (r) => r.status === 200,
  });

  sleep(0.2); // Breve pausa entre eventos generados
}