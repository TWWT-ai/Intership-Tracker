<#
.SYNOPSIS
    Register the daily internship search with Windows Task Scheduler.

.DESCRIPTION
    Creates a scheduled task that runs `main.py daily` every day, then re-applies
    the technical-programmes layout (scripts/daily_then_format.py).

    The task is registered to run ONLY WHEN THE USER IS LOGGED ON, and not
    hidden. That is deliberate: two of the three platforms sit behind
    Cloudflare, whose check is far more likely to pass in a real, visible
    browser session. A task running in session 0 with no desktop would be
    challenged on every run.

    Nothing here stores a password. The task runs as the current user with
    their existing token.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 07:30
    powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Remove
#>

[CmdletBinding()]
param(
    [string]$Time = "08:00",
    [string]$TaskName = "Internship Tracker Daily",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$script = Join-Path $projectRoot "scripts\daily_then_format.py"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

if (-not (Test-Path $python)) {
    throw "Virtualenv not found at $python. Run 'pipenv install --dev' in $projectRoot first."
}
if (-not (Test-Path $script)) {
    throw "daily_then_format.py not found at $script."
}

# Absolute path to the venv interpreter, so the task never depends on PATH,
# on pipenv resolving, or on which shell happens to launch it.
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "`"$script`"" `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) `
    -MultipleInstances IgnoreNew

# Interactive: the run needs a desktop session for the browser-backed sources.
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Replacing existing task '$TaskName'."
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Searches Trackr, Gradcracker and Bright Network, verifies deadlines, and updates data\internships.xlsx. Never submits an application." | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName' to run daily at $Time."
Write-Host "  Command : $python `"$script`""
Write-Host "  Folder  : $projectRoot"
Write-Host ""
Write-Host "Run it now to check it works:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "See the result:"
Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "  Get-Content '$projectRoot\data\logs\*.log' -Tail 40"
Write-Host ""
Write-Host "Note: the task only runs while you are logged on. That is intentional -"
Write-Host "the Cloudflare-protected sources need a real browser session."
