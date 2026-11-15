# Registers Watch-Fletchery.ps1 in Task Scheduler for the current user: at logon and every 10 minutes, hidden.
# Re-running replaces the task. Pass the same parameters Watch-Fletchery.ps1 takes via -WatchArgs if they differ.
param(
    [string]$TaskName = 'Fletchery watchdog',
    [string]$Script = (Join-Path $PSScriptRoot 'Watch-Fletchery.ps1'),
    [string]$WatchArgs = ''
)
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" $WatchArgs"
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $every) -Settings $settings `
    -Description 'Starts the Fletchery supervisor when the backend is down; alerts when passes stall.' -Force | Out-Null
"Registered '$TaskName': $Script every 10 minutes and at logon."
