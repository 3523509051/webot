# 黑屏下的完整功能自测：熄屏 → 三条发送路径 → 观察桥收发 → 恢复
$py = 'c:\webot\bridge\.venv\Scripts\python.exe'
$t0 = Get-Date

Write-Host '  [1] 后台启动虚拟屏熄屏（300 秒）' -ForegroundColor Yellow
Start-Process powershell -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    'c:\webot\scripts\screen-off.ps1', '-Seconds', '300', '-Mode', 'virtual' -WindowStyle Minimized | Out-Null
Start-Sleep -Seconds 22

Write-Host '  [2] 确认黑屏已建立（期望只剩虚拟屏挂载）' -ForegroundColor Yellow
& powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\vdd.ps1 -Action status |
    Select-String -Pattern 'ATTACHED' | ForEach-Object { Write-Host ('      ' + $_.Line.Trim()) }

Write-Host '  [3] 黑屏中跑三条发送路径' -ForegroundColor Yellow
& $py c:\webot\scripts\_probe_ops.py 2>&1 | Out-String -Width 180

Write-Host '  [4] 桥日志（看 send 与有无收到消息）' -ForegroundColor Yellow
Get-Content c:\webot\bridge\bridge.err.log -Encoding Default -Tail 14 | Out-String -Width 190

Write-Host '  [5] 再等 60 秒，看黑屏期间有没有收到群消息并被回复' -ForegroundColor Yellow
Start-Sleep -Seconds 60
& powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\vdd.ps1 -Action status |
    Select-String -Pattern 'ATTACHED' | ForEach-Object { Write-Host ('      ' + $_.Line.Trim()) }
Get-Content c:\webot\bridge\bridge.err.log -Encoding Default -Tail 10 | Out-String -Width 190

Write-Host '  [6] 熄屏日志（看门狗有没有报告回退）' -ForegroundColor Yellow
Get-Content c:\webot\logs\screen-off.log -Encoding UTF8 -Tail 6

Write-Host '  [7] 检查是否进过待机 / 误唤醒' -ForegroundColor Yellow
Get-WinEvent -FilterHashtable @{LogName = 'System'; StartTime = $t0 } -ErrorAction SilentlyContinue |
    Where-Object { $_.ProviderName -eq 'Microsoft-Windows-Kernel-Power' -and $_.Id -in 506, 507, 172 } |
    Select-Object TimeCreated, Id, @{n = 'First'; e = { ($_.Message -split "`r?`n")[0] } } |
    Format-Table -AutoSize | Out-String -Width 150
Write-Host '      （表格为空 = 未进连接待机 ✓）' -ForegroundColor DarkGray

Write-Host '  [8] 恢复内屏' -ForegroundColor Yellow
& powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\screen-off.ps1 -Restore |
    Select-String -Pattern '恢复|检测' | ForEach-Object { Write-Host ('      ' + $_.Line.Trim()) }
