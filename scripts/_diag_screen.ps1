# 诊断：熄屏后系统到底进了什么状态（只读，不改任何设置）
$since = (Get-Date).AddHours(-14)

'===== 1) 支持的睡眠状态 (powercfg /a) ====='
powercfg /a

'===== 2) 近 14 小时 电源/待机 事件 ====='
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$since} -ErrorAction SilentlyContinue |
    Where-Object { $_.ProviderName -match 'Kernel-Power|Power-Troubleshooter|Kernel-General' } |
    Sort-Object TimeCreated |
    Select-Object -First 40 TimeCreated, Id, ProviderName,
        @{n='First'; e={ ($_.Message -split "`r?`n")[0] }} |
    Format-Table -AutoSize | Out-String -Width 200

'===== 3) 待机期间是否断网（网络连通性） ====='
$sub = 'f15576e8-98b7-4186-b944-eafa664402d9'
powercfg /query SCHEME_CURRENT $sub 2>&1 | Select-String -Pattern '电源设置 GUID|当前交流|当前直流' | Out-String -Width 160

'===== 4) 桥日志最后 6 行的真实时间（看有没有时间断层） ====='
$p = 'c:\webot\bridge\bridge.err.log'
if (Test-Path $p) {
    $fs = [System.IO.File]::Open($p, 'Open', 'Read', 'ReadWrite')
    $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::GetEncoding('GBK'))
    $all = $sr.ReadToEnd(); $sr.Close(); $fs.Close()
    $lines = $all -split "`r?`n" | Where-Object { $_ -match '^\d{4}-\d\d-\d\d' }
    ($lines | Select-Object -Last 6) -join "`n"
    '--- 时间断层（相邻两条间隔 > 90 秒的地方）---'
    $ts = $lines | ForEach-Object { if ($_ -match '^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)') { [datetime]$Matches[1] } }
    for ($i = 1; $i -lt $ts.Count; $i++) {
        $gap = ($ts[$i] - $ts[$i-1]).TotalSeconds
        if ($gap -gt 90) { "  {0} -> {1}   断层 {2:N0} 秒" -f $ts[$i-1], $ts[$i], $gap }
    }
}
