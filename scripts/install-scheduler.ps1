[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "AI Trader Unattended Shadow"
)

$ErrorActionPreference = "Stop"
$isAdministrator = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdministrator) {
    throw "One-time administrator permission is required to register a logged-off S4U wake task. Open PowerShell as Administrator and run this script again."
}
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $projectRoot "scripts\start-shadow.ps1"
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Launcher not found: $launcher"
}

# Task Scheduler is only a conservative weekday wakeup. It deliberately does
# not convert or capture an Eastern/local offset. Python's XNYS calendar and
# trusted wall clock own holidays, DST, early closes, and all strategy timing.
# 05:00 local is earlier than 09:25 ET across continental U.S. zones.
$conservativeLocalWake = [datetime]::Today.AddHours(5)

$actionArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $launcher
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $conservativeLocalWake
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12) -MultipleInstances IgnoreNew -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)
$userId = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, "Register conservative weekday wake task at $($conservativeLocalWake.ToString('HH:mm')) local time")) {
    # WakeToRun cannot wake a sleeping computer when the active Windows power
    # plan disables wake timers. Enable them on AC power only; battery wakeups
    # remain disabled to avoid unattended battery drain.
    & powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to enable AC wake timers for the active Windows power plan."
    }
    & powercfg.exe /setactive SCHEME_CURRENT
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to apply the active Windows power plan after enabling AC wake timers."
    }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Wakes the deterministic SHADOW research orchestrator; Python owns the exchange schedule." -Force | Out-Null
    Write-Output "Installed '$TaskName' for unattended S4U execution and enabled AC wake timers. Conservative wake time: $($conservativeLocalWake.ToString('HH:mm')) local. Keep the PC plugged in and powered or sleeping (not shut down); Python/XNYS owns holidays, market timing, and DST."
}
