<#
webot 重启脚本（替代 webot-service.ps1 的 bridge/astr 动作）

用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-restart.ps1            # 全部：Docker → AstrBot → 桥
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-restart.ps1 -Target bridge
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-restart.ps1 -Target astr

与旧脚本的区别：
  1. 每个外部命令（docker 等）都有硬超时。Docker Desktop 没起来时 docker 客户端会一直卡住，
     旧脚本因此会无限等待。
  2. 重启桥之前先确认 AstrBot 在跑：AstrBot 不在，桥永远连不上，等也是白等。
  3. 只停止「桥自己的」进程：可执行文件在 bridge\.venv 里的 python，及其子进程；
     不再按「命令行里有 main.py」去杀所有 python。
  4. 不动微信窗口（不显示、不最小化、不置前），只检查微信是否在运行并给出提示。
  5. 结果写入 logs\restart-last.txt，退出码 0 = 成功，1 = 失败。脚本本身不暂停，
     可以由计划任务 / 远程调用直接执行。

编码：本文件必须是 UTF-8 with BOM（Windows PowerShell 5.1 按 GBK 读无 BOM 文件会乱码）。
#>
[CmdletBinding()]
param(
    [ValidateSet('all', 'bridge', 'astr')]
    [string]$Target = 'all'
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root       = Split-Path -Parent $PSScriptRoot
$BridgeDir  = Join-Path $Root 'bridge'
$Python     = Join-Path $BridgeDir '.venv\Scripts\python.exe'
$BridgeOut  = Join-Path $BridgeDir 'bridge.log'
$BridgeErr  = Join-Path $BridgeDir 'bridge.err.log'
$LogDir     = Join-Path $Root 'logs'
$ResultFile = Join-Path $LogDir 'restart-last.txt'
$DockerDesktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
$Container  = 'webot-astrbot'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$script:Report = New-Object System.Collections.Generic.List[string]

$ProgressFile = Join-Path $LogDir 'restart-progress.txt'
[IO.File]::WriteAllText($ProgressFile, '', (New-Object Text.UTF8Encoding($true)))

function Note([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
    Write-Host $line
    $script:Report.Add($line)
    # 每步立即落盘：进程中途被杀时也能看到停在哪一步
    try { [IO.File]::AppendAllText($ProgressFile, $line + "`r`n", (New-Object Text.UTF8Encoding($false))) } catch { }
}

function Finish([int]$code) {
    $status = if ($code -eq 0) { 'OK' } else { 'FAILED' }
    $script:Report.Insert(0, "result=$status target=$Target time=$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
    [IO.File]::WriteAllLines($ResultFile, $script:Report, (New-Object Text.UTF8Encoding($true)))
    exit $code
}

# 运行外部命令，超时就杀掉。返回 @{ Code; Out; TimedOut }
function Invoke-Timed([string]$File, [string]$Arguments, [int]$TimeoutSec = 20) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $File
    $psi.Arguments = $Arguments
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = $Root
    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    try { [void]$p.Start() } catch { return @{ Code = -1; Out = "$_"; TimedOut = $false } }
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($TimeoutSec * 1000)) {
        try { $p.Kill() } catch { }
        return @{ Code = -1; Out = "timeout after ${TimeoutSec}s"; TimedOut = $true }
    }
    return @{ Code = $p.ExitCode; Out = ($outTask.Result + $errTask.Result).Trim(); TimedOut = $false }
}

function Test-Port([int]$Port, [int]$TimeoutMs = 1500) {
    $c = New-Object Net.Sockets.TcpClient
    try {
        $ar = $c.BeginConnect('127.0.0.1', $Port, $null, $null)
        return ($ar.AsyncWaitHandle.WaitOne($TimeoutMs) -and $c.Connected)
    } catch { return $false } finally { $c.Close() }
}

function Get-OneBotPort {
    try {
        $cfg = Get-Content (Join-Path $BridgeDir 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ("$($cfg.astrbot.ws_url)" -match ':(\d+)') { return [int]$Matches[1] }
    } catch { }
    return 6199
}

# ── Docker ───────────────────────────────────────────────
function Test-DockerEngine {
    $r = Invoke-Timed 'docker' 'info --format {{.ServerVersion}}' 10
    return ($r.Code -eq 0)
}

function Start-DockerEngine {
    if (Test-DockerEngine) { Note 'Docker 引擎在运行'; return $true }
    if (-not (Test-Path $DockerDesktop)) { Note "找不到 Docker Desktop：$DockerDesktop"; return $false }
    Note '启动 Docker Desktop（最多等 180 秒）'
    Start-Process -FilePath $DockerDesktop | Out-Null
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        if (Test-DockerEngine) { Note 'Docker 引擎就绪'; return $true }
    }
    Note 'Docker 引擎启动超时'
    return $false
}

# ── AstrBot ──────────────────────────────────────────────
function Restart-Astrbot([bool]$Recreate) {
    if (-not (Start-DockerEngine)) { return $false }
    if ($Recreate) {
        Note "重启容器 $Container"
        $r = Invoke-Timed 'docker' "restart $Container" 120
    } else {
        Note "确保容器 $Container 在运行"
        $r = Invoke-Timed 'docker' 'compose up -d --no-deps astrbot' 180
    }
    if ($r.Code -ne 0) { Note "docker 命令失败：$($r.Out)"; return $false }
    $port = Get-OneBotPort
    $deadline = (Get-Date).AddSeconds(150)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        if ((Test-Port 6185) -and (Test-Port $port)) { Note "AstrBot 就绪（WebUI 6185、OneBot $port 均可连）"; return $true }
    }
    Note "等 AstrBot 就绪超时（端口 6185 / $port 未就绪）"
    return $false
}

# ── 桥 ──────────────────────────────────────────────────
function Get-BridgeProcessIds {
    $all = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue)
    $venvPy = $Python.ToLower()
    $roots = @($all | Where-Object { "$($_.ExecutablePath)".ToLower() -eq $venvPy -and "$($_.CommandLine)" -match 'main\.py' })
    $ids = New-Object System.Collections.Generic.HashSet[int]
    foreach ($p in $roots) { [void]$ids.Add([int]$p.ProcessId) }
    # venv 的 python.exe 是启动器，真正干活的是它拉起的子进程
    foreach ($p in $all) { if ($ids.Contains([int]$p.ParentProcessId)) { [void]$ids.Add([int]$p.ProcessId) } }
    return @($ids)
}

function Stop-Bridge {
    $ids = Get-BridgeProcessIds
    if ($ids.Count -eq 0) { Note '桥当前未运行'; return }
    foreach ($id in $ids) { try { Stop-Process -Id $id -Force -ErrorAction Stop } catch { } }
    Start-Sleep -Seconds 2
    Note "已停止桥进程：$($ids -join ', ')"
}

function Test-BridgeConnected([int]$Port) {
    foreach ($id in (Get-BridgeProcessIds)) {
        $conn = Get-NetTCPConnection -OwningProcess $id -State Established -ErrorAction SilentlyContinue |
                Where-Object { $_.RemotePort -eq $Port }
        if ($conn) { return $true }
    }
    return $false
}

function Restart-Bridge {
    if (-not (Test-Path $Python)) { Note "找不到桥的虚拟环境：$Python"; return $false }
    $port = Get-OneBotPort
    if (-not (Test-Port $port)) { Note "AstrBot 的 OneBot 端口 $port 不可连：桥启动后会连不上，请先启动 AstrBot（-Target all）"; return $false }
    if (-not (Get-Process Weixin -ErrorAction SilentlyContinue)) {
        Note '微信没有运行：请先打开并登录微信（脚本不会替你操作微信窗口）'
        return $false
    }
    Stop-Bridge
    foreach ($f in @($BridgeOut, $BridgeErr)) {
        if (Test-Path $f) { try { Move-Item -Force -Path $f -Destination ($f + '.1') -ErrorAction Stop } catch { } }
    }
    Note '启动桥（后台，日志 bridge\bridge.err.log）'
    Start-Process -FilePath $Python -ArgumentList 'main.py' -WorkingDirectory $BridgeDir `
        -RedirectStandardOutput $BridgeOut -RedirectStandardError $BridgeErr -WindowStyle Hidden | Out-Null
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        if (Test-BridgeConnected $port) { Note "桥已连上 AstrBot（端口 $port）"; return $true }
        $tail = (Get-Content $BridgeErr -Encoding Default -Tail 60 -ErrorAction SilentlyContinue | Out-String)
        if ($tail -match '初始化微信失败') { Note '桥启动失败：初始化微信失败（微信未登录、窗口不可用或锁屏）'; return $false }
        if ($tail -match '没有可监听的群') { Note '桥启动失败：config.json 没有可监听的群'; return $false }
        if ((Get-BridgeProcessIds).Count -eq 0) { Note '桥进程已退出，见 bridge\bridge.err.log'; return $false }
    }
    Note '等桥连上 AstrBot 超时（90 秒）'
    return $false
}

# ── 主流程 ───────────────────────────────────────────────
Note "webot-restart 开始：Target=$Target"
$ok = $true
switch ($Target) {
    'astr'   { $ok = Restart-Astrbot $true }
    'bridge' { $ok = Restart-Bridge }
    'all'    { $ok = (Restart-Astrbot $false) -and (Restart-Bridge) }
}
if ($ok) {
    $tail = @(Get-Content $BridgeErr -Encoding Default -Tail 30 -ErrorAction SilentlyContinue |
              Where-Object { $_ -match '微信账号|监听|群同步|已连接 AstrBot' } | Select-Object -Last 5)
    foreach ($l in $tail) { Note "日志：$l" }
    Note '完成'
    Finish 0
}
Note '失败（详见上面的步骤）'
Finish 1
