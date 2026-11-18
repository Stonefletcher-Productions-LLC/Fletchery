# Keeps the Fletchery backend running with no window. The scheduled task "Fletchery" (Install-Fletchery.ps1 -AutoStart)
# starts this at sign-in and every 10 minutes (it does nothing while one already runs).
#
# Exit 75 (new code installed, or Restart pressed) starts the backend again at once; exit 77 means another backend
# already runs on the same state folder, so this waits and checks again a minute later (never a second backend); a
# crash starts it again after 5 seconds, and after 4 crashes in 5 minutes it waits 10 minutes and tries again instead of
# giving up: a backend that stays down is the stall people notice hours later (2026-11-14). Exit 0 stays stopped.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File shell\Start-Fletchery.ps1 -Profile C:\path\Project.profile.json
#   Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',
#       '<this file>','-Profile','<profile>','-LogDir','<state folder>'   # detached, outlives the shell
param(
    [Parameter(Mandatory = $true)][Alias('Profile')][string]$ProjectProfile,
    [string]$Python = 'python',
    [string]$Backend = (Join-Path $env:LOCALAPPDATA 'Programs\Fletchery'),
    [string]$LogDir = (Join-Path $env:LOCALAPPDATA 'Fletchery\logs')
)
$ErrorActionPreference = 'Continue'
if (-not (Test-Path $ProjectProfile)) { throw "No profile at $ProjectProfile" }
New-Item -ItemType Directory -Force $LogDir | Out-Null
$env:PYTHONUNBUFFERED = '1'; $env:PYTHONIOENCODING = 'utf-8'; $env:PYTHONUTF8 = '1'
$journal = Join-Path $LogDir 'backend-supervisor.log'
function Note([string]$Text) { "$(Get-Date -Format o) $Text" | Add-Content $journal }

$crashes = @()
while ($true) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $out = Join-Path $LogDir 'backend-stdout.log'; $err = Join-Path $LogDir 'backend-stderr.log'
    foreach ($f in @($out, $err)) {
        if ((Test-Path $f) -and (Get-Item $f).Length -gt 0) { Move-Item $f ($f -replace '\.log$', "-$stamp.log") -Force }
    }
    # keep the newest 10 old logs of each kind; nothing else cleans this folder
    foreach ($kind in 'backend-stdout-*.log', 'backend-stderr-*.log') {
        Get-ChildItem $LogDir -Filter $kind -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending |
            Select-Object -Skip 10 | Remove-Item -ErrorAction SilentlyContinue
    }
    Note 'starting backend'
    $p = Start-Process -FilePath $Python -ArgumentList @('-m', 'fletchery', 'run', '--profile', "`"$ProjectProfile`"") `
        -WorkingDirectory $Backend -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    $null = $p.Handle   # Windows PowerShell only fills ExitCode if the handle was opened while it ran
    try { $p.PriorityClass = 'BelowNormal' } catch {}
    $p.WaitForExit()
    $code = $p.ExitCode
    Note "backend exited $code"
    if ($code -eq 75) { continue }   # new code installed: reload right away
    if ($code -eq 0) { break }       # a clean stop stays stopped
    if ($code -eq 77) { Start-Sleep -Seconds 60; continue }   # another backend runs: never start a second, check again later
    $now = Get-Date
    $crashes = @($crashes | Where-Object { ($now - $_).TotalMinutes -lt 5 }) + $now
    if ($crashes.Count -gt 3) {
        Note '4 crashes in 5 minutes; waiting 10 minutes before starting again'
        Start-Sleep -Seconds 600
        $crashes = @()
        continue
    }
    Start-Sleep -Seconds 5
}
