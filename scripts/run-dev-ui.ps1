# run-dev-ui.ps1 - start the multiACE web UI locally, against a real
# Moonraker if one is reachable and against tests\fixtures\*.json if not.
#
#   .\scripts\run-dev-ui.ps1
#   $env:MOONRAKER_URL = "http://printer:7125"; .\scripts\run-dev-ui.ps1
#   $env:MULTIACE_MOCK_MODE = "1"; .\scripts\run-dev-ui.ps1   # force mock
#   $env:MULTIACE_MOCK_STATE_FILE = "mock_state_head.json"; .\scripts\run-dev-ui.ps1
#       # dashboard in head mode instead of the default multi-ACE fixture
#
# Mock mode also unlocks POST /api/debug/simulate (inject a fake error
# notification) and GET /api/debug/sample-gcode (a small 4-filament
# sample print, tests\fixtures\sample_4color.gcode).

$ErrorActionPreference = "Stop"

$Port = if ($env:PORT) { $env:PORT } else { "7126" }
$MoonrakerUrl = if ($env:MOONRAKER_URL) { $env:MOONRAKER_URL } else { "http://127.0.0.1:7125" }
$RepoRoot = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RepoRoot "multiace\web\backend"

if ($env:MULTIACE_MOCK_MODE) {
    Write-Host "-> mock mode forced by MULTIACE_MOCK_MODE=$($env:MULTIACE_MOCK_MODE)"
} else {
    try {
        Invoke-WebRequest -Uri "$MoonrakerUrl/printer/info" -TimeoutSec 2 -UseBasicParsing | Out-Null
        Write-Host "OK  Moonraker found at $MoonrakerUrl - using real data"
        $env:MULTIACE_MOCK_MODE = "0"
    } catch {
        Write-Host "--  Moonraker not reachable at $MoonrakerUrl - using mock data"
        $env:MULTIACE_MOCK_MODE = "1"
    }
}

$env:MOONRAKER_URL = $MoonrakerUrl
if (-not $env:MULTIACE_MOCK_DIR) {
    $env:MULTIACE_MOCK_DIR = Join-Path $RepoRoot "tests\fixtures"
}
# The installer copies the catalogs next to the backend (web\i18n); in a
# checkout they live at multiace\i18n. Without this every label in the UI
# renders as its raw key.
if (-not $env:MULTIACE_I18N_DIR) {
    $env:MULTIACE_I18N_DIR = Join-Path $RepoRoot "multiace\i18n"
}
# A dev box has no /home/lava; point the config editor at a scratch copy
# so saving from the UI cannot 404 (and cannot touch a real printer).
if (-not $env:MULTIACE_CFG_PATH) {
    $devCfg = Join-Path $RepoRoot ".devdata\ace.cfg"
    $devDir = Split-Path -Parent $devCfg
    if (-not (Test-Path $devDir)) { New-Item -ItemType Directory -Path $devDir | Out-Null }
    if (-not (Test-Path $devCfg)) {
        Copy-Item (Join-Path $RepoRoot "multiace\config\extended\ace.cfg") $devCfg
    }
    $env:MULTIACE_CFG_PATH = $devCfg
}
Write-Host "    config: $($env:MULTIACE_CFG_PATH)"

$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$Url = "http://127.0.0.1:$Port/"

Start-Job -ScriptBlock {
    param($u)
    Start-Sleep -Seconds 2
    Start-Process $u
} -ArgumentList $Url | Out-Null

if ($env:MULTIACE_MOCK_MODE -eq "1") {
    Write-Host "    mock: sample print at http://127.0.0.1:$Port/api/debug/sample-gcode"
}

Write-Host "Dev server on $Url  (Ctrl+C to stop)"
Push-Location $BackendDir
try {
    & $Python -m uvicorn main:app --host 127.0.0.1 --port $Port --reload
} finally {
    Pop-Location
}
