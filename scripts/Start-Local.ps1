param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
$backend = Join-Path $projectRoot 'backend'
$logFolder = Join-Path $projectRoot '_data\desktop\logs'
$homeUrl = 'http://127.0.0.1:18743/desktop/'
$healthUrl = 'http://127.0.0.1:18743/local/api/health'
$mutex = New-Object Threading.Mutex($false, 'Local\AudioLearningAssistantLauncher')
if (-not $mutex.WaitOne(0)) { exit 0 }
try {
    $running = $false
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($health.app -ne 'audio-learning-desktop') { throw 'Port 18743 belongs to another application.' }
        $running = $true
    } catch [System.Net.WebException] { }
    if (-not $running) {
        if (-not (Test-Path -LiteralPath $python)) { throw 'Python environment is missing. See the Local Desktop section in README.md.' }
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'apps\web\dist\index.html'))) { throw 'Web build is missing. Run npm run build in apps/web first.' }
        $null = New-Item -ItemType Directory -Path $logFolder -Force
        $process = Start-Process -FilePath $python -ArgumentList @('-m','app.desktop') -WorkingDirectory $backend -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logFolder 'stdout.log') -RedirectStandardError (Join-Path $logFolder 'stderr.log')
        $ready = $false
        for ($attempt = 0; $attempt -lt 50; $attempt++) {
            if ($process.HasExited) { throw ('Application exited. Read logs: ' + $logFolder) }
            try {
                $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 1
                if ($health.app -eq 'audio-learning-desktop') { $ready = $true; break }
            } catch [System.Net.WebException] { }
            Start-Sleep -Milliseconds 300
        }
        if (-not $ready) { throw ('Application did not become ready. Read logs: ' + $logFolder) }
    }
    $null = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18743/local/api/tts/start' -Headers @{'X-Local-App'='1'} -TimeoutSec 5
    if (-not $NoBrowser) { Start-Process $homeUrl }
    Write-Output ('Audio Learning Assistant: ' + $homeUrl)
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    if (-not $NoBrowser) { Read-Host 'Press Enter to close' }
    exit 1
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
