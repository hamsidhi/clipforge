# Remote-safe deploy of a fast-forward commit into the live checkout (first used 2026-11-09, fix/remote-ig-reach).
# Run detached:  Start-Process powershell -WindowStyle Hidden -ArgumentList "-File deploy.ps1 ..."
# 1. backups (DB via SQLite online backup, config.yaml, frontend/dist)  2. new files from $New (config.yaml untouched)
# 3. frontend build  4. watchdog task installed  5. old server stopped by PID, task started
# 6. 120 s health poll (local + Tailscale URL + a route that only the new code has + scheduler heartbeat)
# 7. healthy -> main ref fast-forwarded; unhealthy -> rollback.ps1 runs by itself.
param(
    [string]$Old = "3847a149eb0a7659be14ca7cc00ecc0842571ca5",
    [string]$New = "e4026b5",
    [string]$Root = "D:\clipforge",
    [string]$TsUrl = "https://leaderhub-ad8.tail8f9785.ts.net",
    [string]$NewRoute = "/api/publishing/reach"
)
$ErrorActionPreference = "Continue"
$Log = "$Root\workspace\logs\deploy_remote_fix.log"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
function Say($m) { $line = "$(Get-Date -Format s) [deploy] $m"; Add-Content -Path $Log -Value $line; Write-Output $line }
function Rollback { Say "UNHEALTHY -> rollback"; & powershell -NoProfile -ExecutionPolicy Bypass -File "$Here\rollback.ps1" -Old $Old -New $New -Root $Root; Say "DEPLOY RESULT: ROLLED BACK"; exit 2 }

Set-Location $Root
$py = "$Root\.venv\Scripts\python.exe"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force "$Root\workspace\backups" | Out-Null
Copy-Item "$Here\rollback.ps1" "$Root\workspace\rollback.ps1" -Force
# 1. backups
& $py -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); print('ok')" "$Root\workspace\clipforge.db" "$Root\workspace\backups\clipforge-pre-remote-fix-$stamp.db" | Out-Null
if (-not (Test-Path "$Root\workspace\backups\clipforge-pre-remote-fix-$stamp.db")) { Say "DB backup FAILED - nothing changed"; exit 1 }
Copy-Item "$Root\config.yaml" "$Root\workspace\backups\config.yaml.pre-remote-fix" -Force
if (-not (Test-Path "$Root\workspace\backups\dist-pre-remote-fix")) { Copy-Item "$Root\frontend\dist" "$Root\workspace\backups\dist-pre-remote-fix" -Recurse }
Say "backups done ($stamp)"
if ((git rev-parse refs/heads/main).Trim() -ne $Old) { Say "main is not at $Old - stop, nothing changed"; exit 1 }
# 2. code (config.yaml stays exactly as it is)
foreach ($l in (git diff --name-status $Old $New)) {
    $st, $f = $l -split "`t", 2
    if ($f -eq "config.yaml") { continue }
    git checkout $New -- $f
}
Say "files from $New in place"
# 3. frontend
Push-Location "$Root\frontend"; $b = cmd /c "npm run build 2>&1"; $ok = $LASTEXITCODE; Pop-Location
if ($ok -ne 0) { Say "frontend build failed: $($b | Select-Object -Last 5)"; Rollback }
Say "frontend built"
# 4. watchdog task
$inst = & $py -m clipforge service install 2>&1
Say "service install: $($inst | Select-Object -Last 2)"
if (-not (Get-ScheduledTask -TaskName "ClipForge Service" -ErrorAction SilentlyContinue)) { Say "watchdog task missing"; Rollback }
# 5. restart
$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    $p = $conn.OwningProcess
    $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$p").ParentProcessId
    $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$parent" -ErrorAction SilentlyContinue
    Stop-Process -Id $p -Force
    if ($pp -and $pp.CommandLine -match "clipforge ui") { Stop-Process -Id $parent -Force -ErrorAction SilentlyContinue }
    Say "old server stopped (PID $p)"
}
for ($i = 0; $i -lt 15; $i++) { if (-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) { break }; Start-Sleep 1 }
$t0 = (Get-Date).ToUniversalTime()
Start-ScheduledTask -TaskName "ClipForge Service"
Say "watchdog task started at $($t0.ToString('s'))Z"
# 6. health
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep 2
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 3
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000$NewRoute" -TimeoutSec 5 -UseBasicParsing
        $ts = Invoke-RestMethod -Uri "$TsUrl/api/health" -TimeoutSec 8
        $st = Get-Content "$Root\workspace\scheduler_state.json" -Raw | ConvertFrom-Json
        $beat = [datetime]::Parse($st.last_run_at).ToUniversalTime()
        if ($h.ok -and $r.StatusCode -eq 200 -and $ts.ok -and $beat -gt $t0) { $healthy = $true; Say "healthy: local ok, new route 200, tailnet ok, scheduler ran $($st.last_run_at)"; break }
    } catch {}
}
if (-not $healthy) { Rollback }
# 7. fast-forward main (config.yaml index entry = the live staged settings, unchanged)
git update-ref refs/heads/main (git rev-parse $New).Trim() $Old
git update-index --cacheinfo "100644,200eb99122adf50e007c21fe1496658de5ba6768,config.yaml"
Say "main -> $New"
Say "DEPLOY RESULT: OK"
exit 0
