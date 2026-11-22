# Run the existing bridge restart entry point in the signed-in desktop session.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskName = 'webot-memory-upgrade-bridge'
$scriptPath = Join-Path $PSScriptRoot 'webot-restart.ps1'
$resolved = [IO.Path]::GetFullPath($scriptPath)
if ($resolved -ne 'C:\webot\scripts\webot-restart.ps1') { throw 'Unexpected restart script path' }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$resolved`" -Target bridge" -WorkingDirectory 'C:\webot'
$principal = New-ScheduledTaskPrincipal -UserId 'maoxiu' -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output 'DESKTOP_RESTART_REQUESTED'
