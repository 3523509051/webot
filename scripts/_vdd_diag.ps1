# 诊断：虚拟屏熄屏状态下，「机器人操作」会不会把内屏拉回来（亮屏）？
# 期间请不要碰鼠标/键盘（碰了会触发我们的真人输入检测 → 正常恢复，诊断就白做了）
$ErrorActionPreference = 'Continue'
$py = 'c:\webot\bridge\.venv\Scripts\python.exe'

function Snap([string]$tag) {
    Write-Host ("  [{0}] {1}" -f (Get-Date).ToString('HH:mm:ss'), $tag) -ForegroundColor Yellow
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\vdd.ps1 -Action status 2>$null |
        Select-String -Pattern 'ATTACHED'
    if (-not $out) { Write-Host '    （没有 ATTACHED 的显示？）' }
    foreach ($l in $out) { Write-Host ("    " + $l.Line.Trim()) }
}

$t0 = Get-Date
Write-Host '  → 后台启动虚拟屏熄屏（90 秒后自动恢复）' -ForegroundColor DarkGray
Start-Process powershell -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    'c:\webot\scripts\screen-off.ps1', '-Seconds', '90' -WindowStyle Minimized | Out-Null
Start-Sleep -Seconds 12

Snap '熄屏后 · 无任何操作'
Start-Sleep -Seconds 6
Snap '再过 6 秒 · 仍无操作'

Write-Host '  → 触发机器人操作（用桥的代码发一条 @ 消息）' -ForegroundColor Yellow
& $py c:\webot\scripts\_probe_send.py 2>&1 | Select-String -Pattern 'send_at|current_chat|发送@' | Out-String -Width 170

Start-Sleep -Seconds 2
Snap '机器人操作刚结束'
Start-Sleep -Seconds 8
Snap '之后 8 秒'

Write-Host '  --- 这期间的电源/显示/PnP 事件 ---' -ForegroundColor Yellow
Get-WinEvent -FilterHashtable @{LogName = 'System'; StartTime = $t0 } -ErrorAction SilentlyContinue |
    Where-Object { $_.ProviderName -match 'Kernel-Power|Kernel-PnP|Display|Win32k|UserModePower' } |
    Sort-Object TimeCreated |
    Select-Object TimeCreated, Id, ProviderName, @{n = 'First'; e = { ($_.Message -split "`r?`n")[0] } } |
    Format-Table -AutoSize | Out-String -Width 175

if (Test-Path c:\webot\logs\screen-off.state) {
    Write-Host '  → 收尾：仍在熄屏状态，主动恢复' -ForegroundColor DarkGray
    & powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\screen-off.ps1 -Restore
} else {
    Write-Host '  → 收尾：已自行恢复（或诊断期间被真人输入打断）' -ForegroundColor DarkGray
}
