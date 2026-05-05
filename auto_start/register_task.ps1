# Indonesia Law RAG - Task Scheduler registration (run once)
# Uses $PSScriptRoot so the file is encoding-agnostic.

$ErrorActionPreference = "Stop"

$TaskName = "Indonesia Law RAG"
$Project  = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Script   = Join-Path $PSScriptRoot "start_rag.ps1"

if (-not (Test-Path $Script)) { throw "start_rag.ps1 not found: $Script" }

$psArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $Script

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $psArgs `
    -WorkingDirectory $Project

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT30S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Indonesia Law RAG: starts uvicorn + cloudflared on logon" `
    -Force | Out-Null

Write-Output "[OK] Task Scheduler registered: $TaskName"
Write-Output ""
Write-Output "Project: $Project"
Write-Output "Script : $Script"
Write-Output ""
Write-Output "Run now to test:"
Write-Output "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Output ""
Write-Output "Status:"
Write-Output "  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Output ""
Write-Output "Unregister:"
Write-Output "  .\auto_start\unregister_task.ps1"
