# Register/update the DailyNewsEmail scheduled task.
# Run elevated (as administrator): right-click setup_task.bat -> Run as administrator,
# or: powershell -ExecutionPolicy Bypass -File register_task.ps1

$ErrorActionPreference = 'Stop'

# Auto-detect python from PATH so the task survives Python upgrades
$py = (Get-Command python).Source
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$action = New-ScheduledTaskAction -Execute $py `
    -Argument (Join-Path $scriptDir 'send_email.py') `
    -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -Daily -At 09:05
# Laptop-friendly: start on battery, don't stop on battery switch,
# run missed tasks as soon as possible after boot/logon
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName 'DailyNewsEmail' `
    -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "Task registered: DailyNewsEmail, daily 09:05"
Write-Host "Interpreter: $py"
Write-Host "Script:      $scriptDir\send_email.py"
