$ErrorActionPreference = 'Stop'
try {
    $null = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18743/local/api/shutdown' -Headers @{'X-Local-App'='1'} -TimeoutSec 10
    Write-Output 'Application is stopping. Its TTS container will stop as well.'
} catch {
    Write-Host ('Unable to stop the local app: ' + $_.Exception.Message) -ForegroundColor Red
    Read-Host 'Press Enter to close'
    exit 1
}
