# 逐步验证虚拟屏链路：挂载 → 切换 → 状态（每步都拍快照）
function S([string]$t) {
    Write-Host ''
    Write-Host "== $t" -ForegroundColor Yellow
    & powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\vdd.ps1 -Action status 2>$null |
        Select-String -Pattern 'USB Mobile Monitor|ATTACHED' |
        ForEach-Object { Write-Host ('   ' + $_.Line.Trim()) }
}

S '① 初始状态'

Write-Host ''
Write-Host '→ 触发 webot-vdd-disable (enableidd 0)' -ForegroundColor DarkGray
Start-ScheduledTask -TaskName 'webot-vdd-disable'
Start-Sleep -Seconds 3
Write-Host '→ 触发 webot-vdd-enable (enableidd 1)' -ForegroundColor DarkGray
Start-ScheduledTask -TaskName 'webot-vdd-enable'
Start-Sleep -Seconds 7
S '② 挂载后（期望：出现 USB Mobile Monitor ... ATTACHED）'

Write-Host ''
Write-Host '→ displayswitch /external' -ForegroundColor DarkGray
& displayswitch.exe /external
Start-Sleep -Seconds 6
S '③ 切换后（期望：DISPLAY1 不再 ATTACHED，只剩虚拟屏）'

Start-Sleep -Seconds 6
Write-Host ''
Write-Host '→ 恢复 displayswitch /internal' -ForegroundColor DarkGray
& displayswitch.exe /internal
Start-Sleep -Seconds 5
S '④ 恢复后'
