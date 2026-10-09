# 测试「持久化切换」：虚拟屏设主屏 + 内屏断开，看能否长期保持（不再被 15 秒回退）
. c:\webot\scripts\dispctl.ps1

function S([string]$t) {
    Write-Host ("  [{0}] {1}" -f (Get-Date).ToString('HH:mm:ss'), $t) -ForegroundColor Yellow
    foreach ($l in [DispCtl]::Snap()) { Write-Host "    $l" }
}

S '① 切换前'
Write-Host '→ Switch-ToVirtual(3840x2160)' -ForegroundColor DarkGray
$st = Switch-ToVirtual 3840 2160
Start-Sleep -Seconds 5
S '② 切换后（期望：只剩虚拟屏）'
Start-Sleep -Seconds 15; S '③ +15 秒'
Start-Sleep -Seconds 20; S '④ +35 秒'
Start-Sleep -Seconds 20; S '⑤ +55 秒'

Write-Host '→ Restore-Internal' -ForegroundColor DarkGray
Restore-Internal $st
Start-Sleep -Seconds 5
S '⑥ 恢复后'
