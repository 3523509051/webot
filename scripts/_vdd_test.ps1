# 虚拟屏真机实测：切到虚拟屏（物理屏黑）→ 跑 UIA/发送探针 → 切回内屏
# 全程约 40 秒；结束后自动恢复 /internal，并汇报这段时间有没有进连接待机。
$ErrorActionPreference = 'Continue'
$py = 'c:\webot\bridge\.venv\Scripts\python.exe'
$t0 = Get-Date

Write-Host '  [1/5] 切到虚拟屏（displayswitch /external）—— 物理屏会黑' -ForegroundColor Yellow
& displayswitch.exe /external
Start-Sleep -Seconds 5
& powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\vdd.ps1 -Action status |
    Select-String -Pattern 'ATTACHED' | Out-String -Width 140

# 关键修正：切过去后 Windows 会给虚拟屏用驱动的默认模式（1024x768），
# 于是微信窗口被挤出屏幕、输入框不可达、@ 必然失败。这里把分辨率顶上去，
# 让窗口完整落在屏幕内。
Write-Host '  [1.5/5] 把虚拟屏分辨率顶到 3840x2160（保证微信窗口完整可见）' -ForegroundColor Yellow
& powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\_vdd_res.ps1 -Mode set -W 3840 -H 2160
Start-Sleep -Seconds 3
& powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\vdd.ps1 -Action status |
    Select-String -Pattern 'ATTACHED' | Out-String -Width 140

Write-Host '  [2/5] UIA 探针（定位微信窗口/输入框并点击）' -ForegroundColor Yellow
& $py c:\webot\scripts\_probe_overlay.py 2>&1 | Select-String -Pattern 'ensure_window|win:|edit|Exists|Offscreen|click|OK|Error|Traceback' | Out-String -Width 190

Write-Host '  [3/5] 用桥的代码发一条 @ 消息到测试群' -ForegroundColor Yellow
& $py c:\webot\scripts\_probe_send.py 2>&1 | Out-String -Width 190

Write-Host '  [4/5] 切回内屏（displayswitch /internal）' -ForegroundColor Yellow
& displayswitch.exe /internal
Start-Sleep -Seconds 4
& powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\vdd.ps1 -Action status |
    Select-String -Pattern 'ATTACHED' | Out-String -Width 140

Write-Host '  [5/5] 这段时间有没有进连接待机 / 断网（Kernel-Power 506/507/172）' -ForegroundColor Yellow
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$t0} -ErrorAction SilentlyContinue |
    Where-Object { $_.ProviderName -eq 'Microsoft-Windows-Kernel-Power' -and $_.Id -in 506, 507, 172, 42, 107 } |
    Sort-Object TimeCreated |
    Select-Object TimeCreated, Id, @{n='First'; e={ ($_.Message -split "`r?`n")[0] }} |
    Format-Table -AutoSize | Out-String -Width 150
Write-Host '  （表格为空 = 物理屏黑着期间系统没有进入任何待机状态 ✓）' -ForegroundColor DarkGray

Write-Host '  --- 桥日志尾部 ---' -ForegroundColor DarkGray
Get-Content c:\webot\bridge\bridge.err.log -Encoding Default -Tail 8 | Out-String -Width 190
