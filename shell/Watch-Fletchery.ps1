# Watchdog for an unattended Fletchery backend. Run it every 10 minutes from Task Scheduler (Register-FletcheryWatch.ps1).
#
# The backend restarts itself after a reload or crash only while its supervisor (Start-Fletchery.ps1 or a project's
# Run-*Backend.ps1) runs. After a reboot, a sign-out or a supervisor that exited, nothing started it again, and nobody
# noticed until the owner looked (LifeShare 2026-11-09 and 2026-11-15). This script:
#   1. asks the dashboard for /api/status; when it answers and its last pass is recent, does nothing;
#   2. when nothing answers and no `fletchery run` process exists, starts the supervisor (never a second backend);
#   3. when a process exists but the dashboard has not answered, or the last pass is stale, for -StaleMinutes, writes an
#      alert line to <State>\alerts.jsonl and, once per stall, comments on the round-alerts issue with gh.
# It never stops a process: a hung backend is the hang guard's job (exit 76), and stopping is left to a person.
param(
    [string]$Supervisor = 'E:\lifeshare-scratch\foreman\Run-FletcheryBackend.ps1',
    [string]$State = 'E:\lifeshare-scratch\foreman',
    [string]$Dashboard = 'http://127.0.0.1:8770',
    [int]$StaleMinutes = 30,
    [string]$Repo = 'Stonefletcher-Productions-LLC/LifeShare',
    [string]$AlertIssueTitle = 'Fletchery round alerts'
)
$ErrorActionPreference = 'Continue'
$log = Join-Path $State 'watchdog.log'
function Note([string]$text) { "$(Get-Date -Format o) $text" | Add-Content $log }

$status = $null
try { $status = Invoke-RestMethod "$Dashboard/api/status" -TimeoutSec 30 } catch {}
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -match 'fletchery\s+run' })
$sup = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -and $_.CommandLine.Contains((Split-Path $Supervisor -Leaf)) })

$stale = $false
if ($status) {
    try {
        $last = [datetime]::Parse($status.last_pass).ToUniversalTime()
        $stale = ((Get-Date).ToUniversalTime() - $last).TotalMinutes -gt $StaleMinutes
    } catch { $stale = $false }
    if (-not $stale) {
        $flag = Join-Path $State 'watchdog-stall.flag'
        if (Test-Path $flag) { Remove-Item $flag -Force; Note 'backend healthy again' }
        exit 0
    }
}

if (-not $status -and $procs.Count -eq 0) {
    if ($sup.Count -gt 0) { Note "no backend process but the supervisor runs (pid $($sup[0].ProcessId)); it restarts it"; exit 0 }
    Note 'backend down and no supervisor: starting the supervisor'
    # A headless console: Windows Terminal ignores -WindowStyle Hidden and would show the supervisor as a window.
    Start-Process (Join-Path $env:WINDIR 'System32\conhost.exe') -WindowStyle Hidden `
        -ArgumentList '--headless', 'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$Supervisor`""
    exit 0
}

# A process exists but the dashboard is silent, or the passes stopped.
$flag = Join-Path $State 'watchdog-stall.flag'
$why = if ($status) { "last pass $($status.last_pass) is older than $StaleMinutes min" } else { "the dashboard has not answered (backend pid $($procs.ProcessId -join ','))" }
$first = -not (Test-Path $flag)
if ($first) { Set-Content $flag (Get-Date -Format o) }
$since = Get-Content $flag -ErrorAction SilentlyContinue
$row = @{ at = (Get-Date).ToUniversalTime().ToString('o'); kind = 'watchdog'; why = $why; since = "$since" } | ConvertTo-Json -Compress
Add-Content (Join-Path $State 'alerts.jsonl') $row
Note "stall: $why"
if ($first) {
    try {
        $n = gh issue list -R $Repo --state open --search "`"$AlertIssueTitle`" in:title" --json number --jq '.[0].number'
        if ($n) {
            $body = Join-Path $env:TEMP 'fletchery-watchdog-comment.md'
            Set-Content $body "Fletchery watchdog: the backend looks stalled: $why. The hang guard restarts a hung backend; if this stays, check $State\backend-supervisor.log." -Encoding utf8
            gh issue comment $n -R $Repo --body-file $body | Out-Null
        }
    } catch { Note "alert comment failed: $_" }
}
