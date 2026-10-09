# 对照实验：停掉 GameViewer（UU 远程）守护服务后，内屏还会不会被拉回？
function S([string]$t) {
    Write-Host ("  [{0}] {1}" -f (Get-Date).ToString('HH:mm:ss'), $t) -ForegroundColor Yellow
    & powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\vdd.ps1 -Action status 2>$null |
        Select-String -Pattern 'ATTACHED' | ForEach-Object { Write-Host ('    ' + $_.Line.Trim()) }
}

Write-Host '→ 停 GameViewer（计划任务，免 UAC）' -ForegroundColor DarkGray
Start-ScheduledTask -TaskName 'webot-gv-stop'
Start-Sleep -Seconds 6
Write-Host ("  GameViewerService 状态: {0}" -f (Get-Service GameViewerService -ErrorAction SilentlyContinue).Status)

S '① 停掉后 · 熄屏前'
& displayswitch.exe /external
Start-Sleep -Seconds 6
& powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\_vdd_res.ps1 -Mode set -W 3840 -H 2160 | Out-Null
S '② 切换后（期望：只剩虚拟屏，内屏消失）'
Start-Sleep -Seconds 15
S '③ +15 秒'
Start-Sleep -Seconds 15
S '④ +30 秒'
Start-Sleep -Seconds 15
S '⑤ +45 秒'

Write-Host '→ 恢复：切回内屏 + 重启 GameViewer' -ForegroundColor DarkGray
& displayswitch.exe /internal
Start-Sleep -Seconds 4
Start-ScheduledTask -TaskName 'webot-gv-start'
Start-Sleep -Seconds 5
Write-Host ("  GameViewerService 状态: {0}" -f (Get-Service GameViewerService -ErrorAction SilentlyContinue).Status)
S '⑥ 恢复后'
