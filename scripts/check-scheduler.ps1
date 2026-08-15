[CmdletBinding()]
param(
    [string]$TaskName = "AI Trader Unattended Shadow"
)

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
$action = @($task.Actions)[0]
$powerQuery = (& powercfg.exe /query SCHEME_CURRENT SUB_SLEEP RTCWAKE 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the active Windows wake-timer setting."
}
$acWakeTimersEnabled = $powerQuery -match '(?im)^\s*Current AC Power Setting Index:\s*0x00000001\s*$'
$valid = $task.Principal.LogonType -eq "S4U" -and $task.Settings.WakeToRun -and $task.Settings.MultipleInstances -eq "IgnoreNew" -and $action.Arguments -like "*start-shadow.ps1*" -and $acWakeTimersEnabled
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State
    NextRunTime = $info.NextRunTime
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
    LogonType = $task.Principal.LogonType
    WakeToRun = $task.Settings.WakeToRun
    AcWakeTimersEnabled = $acWakeTimersEnabled
    MultipleInstances = $task.Settings.MultipleInstances
    ConfigurationValid = $valid
} | Format-List

if (-not $valid) {
    throw "Scheduled task or AC wake-timer configuration does not match the unattended SHADOW safety contract. Re-run install-scheduler.ps1 from an elevated PowerShell."
}
