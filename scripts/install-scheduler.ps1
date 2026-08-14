[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "AI Trader Unattended Shadow"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $projectRoot "scripts\start-shadow.ps1"
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Launcher not found: $launcher"
}

# Task Scheduler only wakes the process. Python's XNYS calendar owns holidays,
# early closes, scan intervals, entry cutoffs, flat time, and EOD timing.
$eastern = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$today = [datetime]::Today
$etWakeUnspecified = [datetime]::SpecifyKind($today.AddHours(9).AddMinutes(25), [DateTimeKind]::Unspecified)
$localWake = [System.TimeZoneInfo]::ConvertTime($etWakeUnspecified, $eastern, [System.TimeZoneInfo]::Local)

$actionArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $launcher
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $localWake
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 9)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, "Register weekday wake task at $($localWake.ToString('HH:mm')) local time")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Wakes the deterministic SHADOW research orchestrator; Python owns the exchange schedule." -Force | Out-Null
    Write-Output "Installed '$TaskName'. Wake time: $($localWake.ToString('HH:mm')) local, corresponding to 09:25 America/New_York today."
}
