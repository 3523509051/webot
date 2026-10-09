# 手动拉取 Ollama 模型（绕过 fake-IP 代理导致的 "redirect target not allowed"）
#
# 背景：代理软件用 fake-IP 模式时，registry.ollama.ai 与 *.cloudflarestorage.com
# 会被解析成 198.18.x.x 保留地址；Ollama 有 SSRF 防护，会拒绝跳转到该地址，
# 导致 `ollama pull` 失败。curl 没有这个检查，所以改由 curl 下载后直接落盘。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File pull_ollama_model.ps1
#   powershell -ExecutionPolicy Bypass -File pull_ollama_model.ps1 -Model nomic-embed-text
#
# 下载完成后用 `ollama list` 应能看到模型。

param(
    [string]$Model = "bge-m3"
)

$ErrorActionPreference = "Stop"
$reg    = "registry.ollama.ai"
$repo   = "library/$Model"
$models = Join-Path $env:USERPROFILE ".ollama\models"
$blobs  = Join-Path $models "blobs"
$manDir = Join-Path $models "manifests\$reg\library\$Model"
New-Item -ItemType Directory -Force -Path $blobs, $manDir | Out-Null

# 1) manifest（必须原样保存）
$manFile = Join-Path $manDir "latest"
$manUrl  = "https://$reg/v2/$repo/manifests/latest"
Write-Host "[1/2] 拉取 manifest: $manUrl"
curl.exe -sS -L -m 120 -o $manFile $manUrl
$man = Get-Content -Raw $manFile | ConvertFrom-Json
Write-Host "      -> $manFile"

# 2) 所有 blob（config + layers）
$digests = @($man.config.digest) + @($man.layers | ForEach-Object { $_.digest })
$i = 0
foreach ($d in $digests) {
    $i++
    $hex = $d -replace '^sha256:', ''
    $out = Join-Path $blobs "sha256-$hex"
    if ((Test-Path $out) -and ((Get-Item $out).Length -gt 0)) {
        Write-Host "[2/2] ($i/$($digests.Count)) 已存在，跳过 $hex"
        continue
    }
    Write-Host "[2/2] ($i/$($digests.Count)) 下载 $hex ..."
    # -C - 支持断点续传，中断后可重跑本脚本
    curl.exe -L -C - -m 7200 -o $out "https://$reg/v2/$repo/blobs/$d"
    if (Test-Path $out) {
        Write-Host ("      -> {0:N1} MB" -f ((Get-Item $out).Length / 1MB))
    }
}

Write-Host ""
Write-Host "完成。执行下面命令确认："
Write-Host "  & (Join-Path `$env:LOCALAPPDATA 'Programs\Ollama\ollama.exe') list"
