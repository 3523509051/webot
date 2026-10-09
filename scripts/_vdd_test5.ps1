# 测试：/external 之后立刻杀掉 DisplaySwitch.exe（它留着「投影」面板 + 全屏覆盖层），
# 看黑屏能否长期保持 —— 若成立，就是最简修复。
. c:\webot\scripts\dispctl.ps1

function S([string]$t) {
    Write-Host ("  [{0}] {1}" -f (Get-Date).ToString('HH:mm:ss'), $t) -ForegroundColor Yellow
    foreach ($l in [DispCtl]::Snap()) { Write-Host "    $l" }
}

S '① 切换前'
& displayswitch.exe /external
Start-Sleep -Seconds 3
$p = Get-Process DisplaySwitch -ErrorAction SilentlyContinue
Write-Host ("  DisplaySwitch 进程: {0}" -f (($p | ForEach-Object { $_.Id }) -join ',')) -ForegroundColor DarkGray
Stop-Process -Name DisplaySwitch -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
S '② 切换后 · 已杀掉 DisplaySwitch'
Start-Sleep -Seconds 15; S '③ +15 秒'
Start-Sleep -Seconds 20; S '④ +35 秒'
Start-Sleep -Seconds 20; S '⑤ +55 秒'

Write-Host '→ 恢复 /internal' -ForegroundColor DarkGray
& displayswitch.exe /internal
Start-Sleep -Seconds 4
S '⑥ 恢复后'
