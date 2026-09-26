# Pruebas de carga y estrés del Pub/Sub con k6 (Windows).
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\run-load-test.ps1 [prueba] [escenario] [vus]
#
#   prueba     e2e (por defecto) | sns
#   escenario  humo | carga (por defecto) | estres | pico
#   vus        usuarios virtuales máximos (opcional)
#
# Ejemplo: .\scripts\run-load-test.ps1 e2e estres 80
# Ver scripts/run-load-test.sh para el detalle de cada prueba y escenario.
param(
    [ValidateSet("e2e", "sns")] [string]$Prueba = "e2e",
    [ValidateSet("humo", "carga", "estres", "pico")] [string]$Escenario = "carga",
    [string]$Vus = ""
)

$Raiz = Split-Path -Parent $PSScriptRoot
$Script = if ($Prueba -eq "e2e") { "e2e-checkout-pubsub.js" } else { "load-test-pubsub.js" }
New-Item -ItemType Directory -Force -Path "$Raiz\tests\k6\resultados" | Out-Null
$Resumen = "resultados/$Prueba-$Escenario-$(Get-Date -Format yyyyMMdd-HHmmss).json"
$K6Args = @("-e", "ESCENARIO=$Escenario", "--summary-export", $Resumen)
if ($Vus) { $K6Args += @("-e", "VUS=$Vus") }

Write-Host "=== k6 · prueba '$Prueba' · escenario '$Escenario' ===" -ForegroundColor Green
Push-Location "$Raiz\tests\k6"
if (Get-Command k6 -ErrorAction SilentlyContinue) {
    k6 run @K6Args $Script
} else {
    Write-Host "k6 no detectado: se usa la imagen grafana/k6 (host.docker.internal)" -ForegroundColor Yellow
    docker run --rm -i -v "${PWD}:/pruebas" -w /pruebas `
        -e BASE_URL="http://host.docker.internal:8080" `
        -e AWS_SNS_ENDPOINT="http://host.docker.internal:4566/" `
        -e MONETIZACION_URL="http://host.docker.internal:8000" `
        grafana/k6 run @K6Args $Script
}
$Codigo = $LASTEXITCODE
Pop-Location

Write-Host "`n=== Estado del Pub/Sub tras la prueba ===" -ForegroundColor Green
try {
    if ($Prueba -eq "e2e") {
        $e = Invoke-RestMethod -Uri "http://localhost:8080/api/bff/pubsub/estado"
        Write-Host "Outbox pendiente: $($e.productor.pendientes) · publicados: $($e.productor.publicados)"
        Write-Host "Cola SQS: $($e.consumidor.colas.principal.visibles) · DLQ: $($e.consumidor.colas.dlq.visibles)"
        Write-Host "Procesados: $($e.consumidor.cobrosProcesados)" -ForegroundColor Yellow
    } else {
        $w = Invoke-RestMethod -Uri "http://localhost:8000/cobros/estado-worker"
        Write-Host "Eventos procesados por el worker: $($w.cobrosProcesados)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "No se pudo consultar el estado. ¿Están activos los port-forwards?" -ForegroundColor Red
}
Write-Host "Resumen k6: tests/k6/$Resumen"
exit $Codigo
