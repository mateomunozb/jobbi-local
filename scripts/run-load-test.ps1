Write-Host "=== Iniciando Prueba de Carga con k6 (JOBBI Local Lab) ===" -ForegroundColor Green

# 1. Verificar si k6 o Docker está disponible
if (Get-Command k6 -ErrorAction SilentlyContinue) {
    Write-Host "Ejecutando k6 nativo..." -ForegroundColor Cyan
    k6 run tests/k6/load-test-pubsub.js
} else {
    Write-Host "k6 no detectado localmente. Ejecutando mediante Docker (host.docker.internal)..." -ForegroundColor Yellow
    
    # En Windows, pasamos la variable de entorno apuntando a host.docker.internal
    docker run --rm -i -e AWS_SNS_ENDPOINT="http://host.docker.internal:4566/" -v "${PWD}:/app" -w /app grafana/k6 run tests/k6/load-test-pubsub.js
}

Write-Host "`n=== Verificando Estado del Microservicio de Monetización ===" -ForegroundColor Green
try {
    $response = Invoke-RestMethod -Uri "http://localhost:8000/cobros"
    Write-Host "Total de eventos asíncronos procesados desde SQS: $($response.total)" -ForegroundColor Yellow
} catch {
    Write-Host "No se pudo consultar http://localhost:8000/cobros. Asegúrate de tener activo el port-forward del servicio." -ForegroundColor Red
}