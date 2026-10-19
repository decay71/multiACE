<#
.SYNOPSIS
Push the working tree to a printer without cutting a release.

.DESCRIPTION
Two push classes; -WebOnly is the default.

  -WebOnly (default)  Syncs multiace/web, multiace/i18n and multiace/tools,
                      restarts the web service, never touches Klipper.
                      The inner development loop: the worst case is a
                      broken web page.

  -Full               Syncs the whole multiace tree and runs
                      install_multiace.sh --install-web, then restarts
                      Klipper. It replaces Klipper extras, so it is gated on
                      the unit tests passing and the printer being idle.

  -Rollback           Restores the installer's own backup of
                      kinematics/extruder.py and restarts Klipper.

  -DryRun             Stages and packs the tree, then prints what would
                      happen. Nothing is copied or run on the printer.

-PrinterHost is required. SSH must already work with a key (no password
prompt): every ssh call runs with BatchMode=yes.

.EXAMPLE
  .\push-to-printer.ps1 -PrinterHost 192.168.1.50
  .\push-to-printer.ps1 -PrinterHost 192.168.1.50 -DryRun
  .\push-to-printer.ps1 -PrinterHost 192.168.1.50 -Full
  .\push-to-printer.ps1 -PrinterHost 192.168.1.50 -Rollback
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PrinterHost,
    [string]$PrinterUser = "root",
    [switch]$WebOnly,
    [switch]$Full,
    [switch]$Rollback,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ($WebOnly -and $Full) { throw "-WebOnly and -Full are mutually exclusive." }

$RepoRoot  = Split-Path -Parent $PSScriptRoot
$RemoteTar = "/tmp/multiace-dev.tar.gz"
$RemoteDir = "/tmp/multiace-dev"
$Mode = if ($Full) { "full" } else { "web-only" }

function Say  ($m) { Write-Host "STATUS: $m" }
function Warn ($m) { Write-Warning $m }
function Die  ($m) { Write-Error $m; exit 1 }

# With $ErrorActionPreference = "Stop", any stderr line from a native .exe
# becomes a terminating error regardless of its exit code. Run native
# commands with "Continue" and judge success from $LASTEXITCODE instead.
function Invoke-NativeChecked {
    param([Parameter(Mandatory)][scriptblock]$Script)
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Script } finally { $ErrorActionPreference = $prevEAP }
}

function Invoke-Remote ($Command) {
    # ssh ships the string to the printer's shell byte for byte, and a stray
    # CR from a CRLF here-string breaks lines like "set -e`r". Strip it.
    $Command = $Command -replace "`r", ""
    if ($DryRun) {
        Write-Host "DRY-RUN: ssh $PrinterUser@$PrinterHost $Command"
        return ""
    }
    $out = Invoke-NativeChecked { ssh -o BatchMode=yes "$PrinterUser@$PrinterHost" $Command 2>&1 }
    if ($LASTEXITCODE -ne 0) { Die "remote command failed ($LASTEXITCODE): $out" }
    return ($out -join "`n")
}

function Get-PrintState {
    try {
        $r = Invoke-RestMethod -TimeoutSec 5 `
            -Uri "http://$PrinterHost/printer/objects/query?print_stats"
        return [string]$r.result.status.print_stats.state
    } catch {
        return ""
    }
}

# Fails closed: "cannot tell" is not the same answer as "idle".
function Assert-PrinterIdle {
    $state = Get-PrintState
    if (-not $state) { $state = "unknown" }
    if ($state -in @("printing", "paused", "unknown")) {
        Die ("printer is '$state'. A full install or rollback restarts Klipper " +
             "and would abort the print. Wait for it to finish.")
    }
    Say "printer state: $state"
}

function Invoke-LocalTests {
    Say "running the unit tests (a tree that fails tests is not pushed)"
    if ($DryRun) { Write-Host "DRY-RUN: python -m pytest tests/unit -q"; return }
    Push-Location $RepoRoot
    try {
        Invoke-NativeChecked { python -m pytest tests/unit -q }
        if ($LASTEXITCODE -ne 0) { Die "tests failed - not pushing." }
    } finally { Pop-Location }
}

function Restart-Klipper {
    Say "restarting Klipper"
    # Not firmware_restart: that reloads the config without re-importing
    # already-loaded extras modules, so changed .py files would sit unused.
    try { Invoke-Remote '/etc/init.d/S60klipper restart' | Out-Null }
    catch { Warn "S60klipper restart failed - check the printer" }
}

function Restart-Web {
    Say "restarting the multiACE web service"
    Invoke-Remote '/etc/init.d/S98multiace-web restart >/dev/null 2>&1 || true' | Out-Null
}

function Confirm-Recovery {
    if ($DryRun) { return }
    Say "waiting for the printer to come back"
    $up = $false
    foreach ($i in 1..60) {
        try {
            $h = Invoke-RestMethod -TimeoutSec 3 -Uri "http://$PrinterHost/multiace/api/health"
            if ($h.status -eq "ok") { $up = $true; Say "multiACE web is up"; break }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if (-not $up) {
        Warn ("multiACE web did not answer within 2 minutes. If this push broke " +
              "something: .\push-to-printer.ps1 -PrinterHost $PrinterHost -Rollback")
    }
}

function Invoke-Rollback {
    Say "restoring the installer's backup of extruder.py"
    $script = @'
set -e
K=/home/lava/klipper/klippy/kinematics
if [ ! -f "$K/extruder_pre_multiace.py" ]; then
    echo "ERROR: no extruder_pre_multiace.py backup found - nothing to roll back" >&2
    exit 1
fi
cp "$K/extruder_pre_multiace.py" "$K/extruder.py"
echo "STATUS: restored extruder.py"
'@
    Invoke-Remote $script | Out-Null
    Restart-Klipper
    Say "rollback done"
}

# --- main ------------------------------------------------------------------

if ($Rollback) {
    Say "target: $PrinterUser@$PrinterHost  class: rollback"
    Assert-PrinterIdle
    Invoke-Rollback
    Confirm-Recovery
    exit 0
}

Say "target: $PrinterUser@$PrinterHost  class: $Mode"
if ($Mode -eq "full") {
    Say "FULL push: replaces Klipper extras and restarts Klipper."
    Invoke-LocalTests
    Assert-PrinterIdle
} else {
    Say "WEB-ONLY push: web, i18n and tools only. Klipper is not touched."
    # A panel that blinks mid-print is bad UX, not damage: warn, do not refuse.
    $state = Get-PrintState
    if ($state -in @("printing", "paused")) {
        Warn "printer is $state - the panel will blink, nothing mechanical is touched"
    }
}

$Stage = Join-Path ([System.IO.Path]::GetTempPath()) ("multiace-push-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Stage -Force | Out-Null
try {
    Say "staging the working tree"
    $sources = if ($Mode -eq "full") { @("multiace") } else { @("multiace/web", "multiace/tools", "multiace/i18n") }
    foreach ($rel in $sources) {
        $src = Join-Path $RepoRoot ($rel -replace "/", "\")
        $dst = Join-Path $Stage ($rel -replace "/", "\")
        New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
        Copy-Item -Recurse -Force $src $dst
    }
    Get-ChildItem -Path $Stage -Recurse -Force -Directory `
        | Where-Object { $_.Name -eq "__pycache__" } | Remove-Item -Recurse -Force
    Get-ChildItem -Path $Stage -Recurse -Force -Filter "*.pyc" | Remove-Item -Force

    # busybox ash fails a `#!/bin/bash\r` shebang with a bare "required file
    # not found", so strip CRLF from everything the printer executes or
    # parses. Write without a BOM, which breaks a shebang the same way.
    $noBom = New-Object System.Text.UTF8Encoding $false
    Get-ChildItem -Path $Stage -Recurse -Force -File `
            -Include '*.sh','*.cfg','*.py','*.json','*.sudoers','post-commit','S[0-9][0-9]*' `
        | ForEach-Object {
            $text = [System.IO.File]::ReadAllText($_.FullName)
            if ($text.Contains("`r`n")) {
                [System.IO.File]::WriteAllText($_.FullName, ($text -replace "`r`n", "`n"), $noBom)
            }
        }

    Say "packing"
    $tarball = Join-Path $Stage "multiace-dev.tar.gz"
    # Git for Windows ships a GNU tar that reads "C:\..." as host:path, so
    # prefer the Windows bsdtar when it exists.
    $tarExe = Join-Path $env:SystemRoot "System32\tar.exe"
    if (-not (Test-Path $tarExe)) { $tarExe = "tar" }
    Invoke-NativeChecked { & $tarExe -C $Stage -czf $tarball multiace }
    if ($LASTEXITCODE -ne 0) { Die "tar failed - is bsdtar/tar on PATH?" }

    if ($DryRun) {
        $mb = [math]::Round((Get-Item $tarball).Length / 1MB, 2)
        Say "dry run: would copy $mb MB to $PrinterHost ($Mode)"
        exit 0
    }

    Say "copying to ${PrinterHost}:$RemoteTar"
    # Not scp: its SFTP and legacy modes crashed sshd on the printer. Stream
    # the file over a plain ssh exec channel as base64 instead. The base64
    # goes through a file and cmd's `<` redirection, because piping one very
    # long string through PowerShell 5.1 into a native process truncates it.
    $b64 = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($tarball))
    $b64File = Join-Path $Stage "payload.b64"
    [System.IO.File]::WriteAllText($b64File, $b64, (New-Object System.Text.ASCIIEncoding))
    Invoke-NativeChecked {
        cmd /c "ssh -T -o BatchMode=yes $PrinterUser@$PrinterHost `"base64 -d > $RemoteTar`" < `"$b64File`""
    }
    if ($LASTEXITCODE -ne 0) { Die "streaming copy to the printer failed" }

    Say "extracting"
    Invoke-Remote "rm -rf '$RemoteDir' && mkdir -p '$RemoteDir' && tar -C '$RemoteDir' -xzf '$RemoteTar'" | Out-Null

    if ($Mode -eq "full") {
        Say "running install_multiace.sh --install-web"
        Invoke-Remote "cd '$RemoteDir/multiace' && chmod +x install_multiace.sh && ./install_multiace.sh --install-web"
        Restart-Klipper
        Confirm-Recovery
    } else {
        # Same destinations and layout as install_multiace.sh uses for these
        # directories (multiace/i18n lands in multiace_web/i18n, where the
        # backend looks for it).
        Say "syncing the web files in place (no installer, no Klipper restart)"
        $sync = @"
set -e
WEB_DEST=/home/lava/multiace_web
if [ ! -d "`$WEB_DEST/backend" ]; then
    echo 'ERROR: '"`$WEB_DEST"' not found - multiACE Web is not installed on this printer. Run a -Full push first.' >&2
    exit 1
fi
mkdir -p "`$WEB_DEST/backend" "`$WEB_DEST/frontend" "`$WEB_DEST/i18n"
cp -a '$RemoteDir/multiace/web/backend/.'  "`$WEB_DEST/backend/"
cp -a '$RemoteDir/multiace/web/frontend/.' "`$WEB_DEST/frontend/"
cp -a '$RemoteDir/multiace/i18n/.'         "`$WEB_DEST/i18n/"
rm -rf "`$WEB_DEST/backend/__pycache__" 2>/dev/null || true
chown -R lava:lava "`$WEB_DEST" 2>/dev/null || true
mkdir -p /home/lava/printer_data/config/tools
cp -f '$RemoteDir/multiace/tools/'*.py /home/lava/printer_data/config/tools/
echo 'STATUS: synced backend, frontend, i18n and tools'
"@
        Invoke-Remote $sync | Out-Null
        Restart-Web
        Confirm-Recovery
    }

    Say "done"
} finally {
    Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
}
