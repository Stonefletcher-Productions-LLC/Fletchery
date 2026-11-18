# Installs or updates Fletchery for this Windows user (no administrator rights): builds Fletchery.exe when its source
# changed, copies the window, the backend (fletchery\), the page (ui\) and the supervisor (shell\) to
# %LOCALAPPDATA%\Programs\Fletchery, checks Python, sets the project profile, and adds Desktop and Start menu shortcuts.
# Re-run it to update: the running backend notices the new code and restarts itself in a few seconds.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File shell\Install-Fletchery.ps1 -Profile C:\path\Project.profile.json -AutoStart
#
# -AutoStart registers ONE scheduled task for this user, "Fletchery": at sign-in and every 10 minutes it starts the
# supervisor (Start-Fletchery.ps1) unless it already runs. The supervisor keeps the backend up (restarts after an update
# or a crash) with no window. The desktop window then only attaches to that backend and never starts its own.
#
# A first install needs -Profile. The installer never invents a profile.
# -Profile is bound to $ProjectProfile: $Profile is a PowerShell automatic variable.
param([Alias('Profile')][string]$ProjectProfile, [string]$Python, [switch]$Start, [switch]$NoShortcuts, [switch]$AutoStart)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path $PSScriptRoot -Parent
$Dest = Join-Path $env:LOCALAPPDATA 'Programs\Fletchery'
$AppData = Join-Path $env:APPDATA 'Fletchery'
New-Item -ItemType Directory -Force $Dest, (Join-Path $AppData 'Projects') | Out-Null

# --- settings first: refuse before copying anything when there is no profile ---
$SettingsFile = Join-Path $AppData 'settings.json'
$Settings = if (Test-Path $SettingsFile) { Get-Content $SettingsFile -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
if (-not $ProjectProfile -and -not ($Settings.profile -and (Test-Path $Settings.profile))) {
    throw 'No project profile yet. Run this again with -Profile <path to Project.profile.json> (see examples\LifeShare.profile.json).'
}

# --- build the window when its source is newer than the built one ---
$Exe = Join-Path $PSScriptRoot 'bin\Fletchery.exe'
$Source = Join-Path $PSScriptRoot 'Fletchery.cs'
if (-not (Test-Path $Exe) -or (Get-Item $Source).LastWriteTimeUtc -gt (Get-Item $Exe).LastWriteTimeUtc) {
    & (Join-Path $PSScriptRoot 'Build-Fletchery.ps1')
}

# --- Python 3.10+ ---
function Find-Python {
    foreach ($Candidate in @('py', 'python')) {
        $C = Get-Command $Candidate -ErrorAction SilentlyContinue
        if (-not $C -or $C.Source -like '*\WindowsApps\*') { continue }  # the Store placeholder is not Python
        try {
            # Windows PowerShell 5.1 turns a native program's stderr into an error under 'Stop': ask quietly
            $Exe = if ($Candidate -eq 'py') { & py -3 -c 'import sys; print(sys.executable)' 2>&1 } else { & python -c 'import sys; print(sys.executable)' 2>&1 }
            $Exe = "$($Exe | Select-Object -Last 1)".Trim()
            if ($LASTEXITCODE -eq 0 -and $Exe -and (Test-Path $Exe)) { return $Exe }
        } catch { }
    }
    return $null
}
if (-not $Python) { $Python = Find-Python }
if (-not $Python -or -not (Test-Path $Python)) { throw 'Python 3.10 or newer was not found. Install it from python.org (tick "Add to PATH"), then run this again, or pass -Python <path>.' }
# no double quotes inside: Windows PowerShell 5.1 drops them when it passes arguments to a native program
$Ver = & $Python -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))"
if (-not $Ver -or [version]$Ver -lt [version]'3.10') { throw "Fletchery needs Python 3.10 or newer; $Python is $Ver." }

# --- copy the app ---
# Only files that changed. A file in use (Fletchery.exe and its DLLs while the window is open) cannot be overwritten but
# can be renamed: move it aside and copy the new one in; the window keeps running the old one until it is reopened.
Get-ChildItem $Dest -Filter '*.old' -ErrorAction SilentlyContinue | Remove-Item -ErrorAction SilentlyContinue
$Reopen = $false
foreach ($F in Get-ChildItem (Join-Path $PSScriptRoot 'bin') -File) {
    $T = Join-Path $Dest $F.Name
    if ((Test-Path $T) -and (Get-FileHash $T).Hash -eq (Get-FileHash $F.FullName).Hash) { continue }
    try { Copy-Item $F.FullName $T -Force -ErrorAction Stop }
    catch {
        Move-Item $T "$T.$([guid]::NewGuid().ToString('N').Substring(0, 8)).old" -Force
        Copy-Item $F.FullName $T -Force
        $Reopen = $true
    }
}
foreach ($Part in 'fletchery', 'ui', 'shell') {
    $Target = Join-Path $Dest $Part
    New-Item -ItemType Directory -Force $Target | Out-Null
    # robocopy: only changed files, so the running backend sees a real change (and restarts) only on an update
    robocopy (Join-Path $Repo $Part) $Target /MIR /XD __pycache__ bin packages node_modules /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $Part failed (robocopy $LASTEXITCODE)." }
}
$global:LASTEXITCODE = 0

# --- settings: profile, Python, backend folder (other keys are kept) ---
if ($ProjectProfile) {
    if (-not (Test-Path $ProjectProfile)) { throw "Profile not found: $ProjectProfile" }
    $Name = (Get-Content $ProjectProfile -Raw | ConvertFrom-Json).name
    $Copy = Join-Path $AppData "Projects\$Name.profile.json"
    if ((Resolve-Path $ProjectProfile).Path -ne $Copy) { Copy-Item $ProjectProfile $Copy -Force }
    $Settings | Add-Member -NotePropertyName profile -NotePropertyValue $Copy -Force
}
$Settings | Add-Member -NotePropertyName python -NotePropertyValue $Python -Force
$Settings | Add-Member -NotePropertyName backend -NotePropertyValue $Dest -Force
if ($AutoStart) { $Settings | Add-Member -NotePropertyName managedBackend -NotePropertyValue $true -Force }
[IO.File]::WriteAllText($SettingsFile, ($Settings | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))

# --- one scheduled task keeps the backend running ---
if ($AutoStart) {
    $Super = Join-Path $Dest 'shell\Start-Fletchery.ps1'
    $TaskArgs = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Super`" -Profile `"$($Settings.profile)`" -Python `"$Python`" -Backend `"$Dest`""
    # conhost --headless: with Windows Terminal as the default terminal, -WindowStyle Hidden alone still flashed a window
    $Action = New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR 'System32\conhost.exe') -Argument $TaskArgs
    $Logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $Every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
    $TaskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    $Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'Fletchery' -Action $Action -Trigger @($Logon, $Every) -Settings $TaskSettings -Principal $Principal `
        -Description 'Keeps the Fletchery backend running (Start-Fletchery.ps1). Started at sign-in and checked every 10 minutes.' -Force | Out-Null
    Start-ScheduledTask -TaskName 'Fletchery'
    Write-Host "Scheduled task 'Fletchery' registered and started."
}

# --- shortcuts ---
if (-not $NoShortcuts) {
    $Shell = New-Object -ComObject WScript.Shell
    foreach ($Dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'))) {
        $Lnk = $Shell.CreateShortcut((Join-Path $Dir 'Fletchery.lnk'))
        $Lnk.TargetPath = Join-Path $Dest 'Fletchery.exe'
        $Lnk.WorkingDirectory = $Dest
        $Lnk.Description = 'Fletchery: live view of all development, build machines and builds'
        $Lnk.Save()
    }
}
Write-Host "Installed Fletchery in $Dest (Python $Ver at $Python; profile $($Settings.profile))"
if ($Reopen) { Write-Host 'Fletchery.exe was open: quit it from the tray and open it again for the new window. The backend and page are already updated.' }
if ($Start) { Start-Process (Join-Path $Dest 'Fletchery.exe') }
