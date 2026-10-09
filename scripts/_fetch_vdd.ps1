# 下载并验签「虚拟显示器驱动」候选包（只下载，不安装）
# 目的：虚拟屏方案的前置评估 —— 驱动能不能在本机装（签名强制是否放行）。
$ErrorActionPreference = 'Continue'
$root = 'c:\webot\tools\vdd'
New-Item -ItemType Directory -Force -Path $root | Out-Null

$candidates = @(
    @{ name = 'amyuni'; url = 'https://www.amyuni.com/downloads/usbmmidd_v2.zip' },
    @{ name = 'nullsignal'; url = 'https://github.com/IYATT-yx/NullSignal/archive/refs/heads/master.zip' }
)

foreach ($c in $candidates) {
    $zip = Join-Path $root ($c.name + '.zip')
    if (Test-Path $zip) { continue }
    Write-Host ("  下载 {0} ..." -f $c.name) -ForegroundColor DarkGray
    try {
        Invoke-WebRequest -Uri $c.url -OutFile $zip -UseBasicParsing -TimeoutSec 90
        Write-Host ("    OK  {0:N0} bytes" -f (Get-Item $zip).Length) -ForegroundColor Green
    } catch {
        Write-Host ("    失败：{0}" -f $_.Exception.Message) -ForegroundColor Yellow
    }
}

# 解包
foreach ($c in $candidates) {
    $zip = Join-Path $root ($c.name + '.zip')
    $out = Join-Path $root $c.name
    if ((Test-Path $zip) -and -not (Test-Path $out)) {
        try { Expand-Archive -Path $zip -DestinationPath $out -Force } catch { Write-Host "  解包失败：$($_.Exception.Message)" -ForegroundColor Yellow }
    }
}

Write-Host ''
Write-Host '  === 包内文件 ===' -ForegroundColor DarkGray
Get-ChildItem -Path $root -Recurse -Include *.inf, *.sys, *.cat, *.exe, *.dll -ErrorAction SilentlyContinue |
    Select-Object @{n='File'; e={ $_.FullName.Replace($root, '') }}, @{n='KB'; e={ [int]($_.Length / 1KB) }} |
    Format-Table -AutoSize | Out-String -Width 160

Write-Host '  === 驱动/安装包签名 ===' -ForegroundColor DarkGray
Get-ChildItem -Path $root -Recurse -Include *.sys, *.cat, deviceinstaller*.exe -ErrorAction SilentlyContinue |
    ForEach-Object {
        $s = Get-AuthenticodeSignature $_.FullName
        [pscustomobject]@{
            File   = $_.Name
            Status = $s.Status
            Signer = if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '-' }
        }
    } | Format-List | Out-String -Width 200

Write-Host '  === 当前是否开启驱动签名强制 / 测试签名 ===' -ForegroundColor DarkGray
bcdedit /enum '{current}' | Select-String -Pattern 'testsigning|nointegritychecks'
Write-Host '  （上面没有输出 = 未开启测试签名模式）'
