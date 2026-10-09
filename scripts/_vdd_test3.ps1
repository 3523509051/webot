# 对照实验 2：只 /external，**不动分辨率**，看黑屏能撑多久；
# 同时列出顶层窗口，找 Windows 的「保留这些显示设置?」确认框。
$py = 'c:\webot\bridge\.venv\Scripts\python.exe'

function S([string]$t) {
    Write-Host ("  [{0}] {1}" -f (Get-Date).ToString('HH:mm:ss'), $t) -ForegroundColor Yellow
    & powershell -NoProfile -ExecutionPolicy Bypass -File c:\webot\scripts\vdd.ps1 -Action status 2>$null |
        Select-String -Pattern 'ATTACHED' | ForEach-Object { Write-Host ('    ' + $_.Line.Trim()) }
    $w = & $py -c "import uiautomation as a
r = a.GetRootControl()
names = [w.Name for w in r.GetChildren() if w.Name]
print(' | '.join(names[:14]))" 2>$null
    if ($w) { Write-Host ("    窗口: " + ($w -join ' ').Trim()) -ForegroundColor DarkGray }
}

S '① 熄屏前'
Write-Host '→ displayswitch /external（不设分辨率）' -ForegroundColor DarkGray
& displayswitch.exe /external
Start-Sleep -Seconds 4
S '② 切换后'
Start-Sleep -Seconds 12; S '③ +12 秒'
Start-Sleep -Seconds 12; S '④ +24 秒'
Start-Sleep -Seconds 12; S '⑤ +36 秒'
Start-Sleep -Seconds 12; S '⑥ +48 秒'

Write-Host '→ 恢复 /internal' -ForegroundColor DarkGray
& displayswitch.exe /internal
Start-Sleep -Seconds 4
S '⑦ 恢复后'
