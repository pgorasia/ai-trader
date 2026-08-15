[CmdletBinding()]
param(
    [string]$TaskName = "AI Trader Unattended Shadow"
)

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
$action = @($task.Actions)[0]
$valid = $task.Principal.LogonType -eq "S4U" -and $task.Settings.WakeToRun -and $task.Settings.MultipleInstances -eq "IgnoreNew" -and $action.Arguments -like "*start-shadow.ps1*"
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State
    NextRunTime = $info.NextRunTime
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
    LogonType = $task.Principal.LogonType
    WakeToRun = $task.Settings.WakeToRun
    MultipleInstances = $task.Settings.MultipleInstances
    ConfigurationValid = $valid
} | Format-List

if (-not $valid) {
    throw "Scheduled task exists but does not match the unattended SHADOW safety contract."
}
