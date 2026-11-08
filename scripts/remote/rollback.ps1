# ClipForge rollback for a remote deploy (first used 2026-11-09 for fix/remote-ig-reach).
# Puts back the previous code (-Old commit) and the previous frontend build, then restarts the web app through the
# watchdog task (or start-ui.bat if there is none). config.yaml is NEVER touched (it holds the live settings).
# The DB is restored only with -RestoreDb (no migration in this change; restoring loses queue updates made since).
param(
    [string]$Old = "3847a149eb0a7659be14ca7cc00ecc0842571ca5",
    [string]$New = "e4026b5",
    [string]$Root = "D:\clipforge",
    [string]$StagedConfigBlob = "200eb99122adf50e007c21fe1496658de5ba6768",
    [switch]$RestoreDb
)
$ErrorActionPreference = "Continue"
$Log = "$Root\workspace\logs\deploy_remote_fix.log"
function Say($m) { $line = "$(Get-Date -Format s) [rollback] $m"; Add-Content -Path $Log -Value $line; Write-Output $line }

function Restart-ClipForge {
    # Windows reports the listener on 8000 as PID 0 here, so find ClipForge by its exact command line (server
    # `-m clipforge ui` + watchdog `-m clipforge service run`), stop those PIDs, then start the watchdog task fresh.
    Stop-ScheduledTask -TaskName "ClipForge Service" -ErrorAction SilentlyContinue
    $procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '-m clipforge (ui|service run)' }
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; Say "stopped PID $($p.ProcessId)" }
    for ($i = 0; $i -lt 20; $i++) { if (-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) { break }; Start-Sleep 1 }
    if (Get-ScheduledTask -TaskName "ClipForge Service" -ErrorAction SilentlyContinue) { Start-ScheduledTask -TaskName "ClipForge Service"; Say "watchdog task started" }
    else { Start-Process -FilePath "$Root\start-ui.bat" -WorkingDirectory $Root; Say "started start-ui.bat (no watchdog task)" }
}

Set-Location $Root
Say "rolling back $New -> $Old"
foreach ($l in (git diff --name-status $Old $New)) {
    $st, $f = $l -split "`t", 2
    if ($f -eq "config.yaml") { continue }
    if ($st -eq "A") { git rm -q --cached --ignore-unmatch -- $f; Remove-Item -LiteralPath (Join-Path $Root $f) -Force -ErrorAction SilentlyContinue }
    else { git checkout $Old -- $f }
}
$cur = (git rev-parse refs/heads/main).Trim()
if ($cur -ne $Old) { git update-ref refs/heads/main $Old $cur; Say "main ref back to $Old" }
git update-index --cacheinfo "100644,$StagedConfigBlob,config.yaml"   # the live config stays staged as before
if (Test-Path "$Root\workspace\backups\dist-pre-remote-fix") {
    Remove-Item "$Root\frontend\dist" -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item "$Root\workspace\backups\dist-pre-remote-fix" "$Root\frontend\dist" -Recurse
    Say "frontend dist restored"
}
$db = $null
if ($RestoreDb) { $db = Get-ChildItem "$Root\workspace\backups\clipforge-pre-remote-fix-*.db" | Sort-Object LastWriteTime | Select-Object -Last 1 }
if ($db) {
    Stop-ScheduledTask -TaskName "ClipForge Service" -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '-m clipforge (ui|service run)' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 3
    Copy-Item $db.FullName "$Root\workspace\clipforge.db" -Force
    Remove-Item "$Root\workspace\clipforge.db-wal", "$Root\workspace\clipforge.db-shm" -ErrorAction SilentlyContinue
    Say "DB restored from $($db.Name)"
}
Restart-ClipForge
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep 2
    try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 3; if ($h.ok) { Say "healthy after rollback (rev $($h.rev))"; exit 0 } } catch {}
}
Say "STILL NOT HEALTHY after rollback - needs attention"
exit 1
