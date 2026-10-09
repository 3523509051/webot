<#
微信 AGI 群聊助手 · 服务管理脚本

用法（推荐双击仓库根目录的 start.cmd / stop.cmd）：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 start
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 stop
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 restart
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 status
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 bridge   # 只重启微信桥（不动 AstrBot）
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\webot-service.ps1 astr     # 只重启 AstrBot（人格/配置/插件改动生效）

它做什么：
    1. 确保 Docker 引擎在跑（没跑就拉起 Docker Desktop 并等它就绪）
    2. docker compose up -d --no-deps astrbot
    3. 等 AstrBot 的 OneBot 适配器连上
    4. 拉起微信桥 bridge\main.py（先清掉残留实例，避免两个桥同时上报）
    5. 等桥连上 AstrBot，最后打印状态

⚠️ 编码注意：本文件必须是 **UTF-8 with BOM**。
   Windows PowerShell 5.1 对无 BOM 的 .ps1 会按系统 ANSI(GBK) 解码，中文会乱码。
   如需修改，请用「另存为 → UTF-8 带 BOM」。
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'restart', 'status', 'bridge', 'astr')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# ── 路径 ──────────────────────────────────────────────
$Root          = Split-Path -Parent $PSScriptRoot          # 仓库根 = scripts 的上一级
$BridgeDir     = Join-Path $Root 'bridge'
$Python        = Join-Path $BridgeDir '.venv\Scripts\python.exe'
$BridgeOut     = Join-Path $BridgeDir 'bridge.log'
$BridgeErr     = Join-Path $BridgeDir 'bridge.err.log'
$DockerDesktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
$WeixinExe     = 'C:\Program Files\Tencent\Weixin\Weixin.exe'
$WebuiUrl      = 'http://localhost:6185'

# ── 输出小工具 ────────────────────────────────────────
function Say  ([string]$m) { Write-Host $m }
function Step ([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok   ([string]$m) { Write-Host "    [OK]   $m" -ForegroundColor Green }
function Warn ([string]$m) { Write-Host "    [注意] $m" -ForegroundColor Yellow }
function Fail ([string]$m) { Write-Host "    [失败] $m" -ForegroundColor Red }
function Hint ([string]$m) { Write-Host "           $m" -ForegroundColor DarkGray }

# ── Docker ────────────────────────────────────────────
function Test-DockerEngine {
    docker info --format '{{.ServerVersion}}' *> $null
    return ($LASTEXITCODE -eq 0)
}

function Start-DockerEngine {
    if (Test-DockerEngine) { Ok 'Docker 引擎已在运行'; return $true }
    if (-not (Test-Path $DockerDesktop)) {
        Fail "找不到 Docker Desktop：$DockerDesktop"
        Hint '请手动打开 Docker Desktop 后重试'
        return $false
    }
    Say '    正在启动 Docker Desktop（通常要 30~90 秒，别关窗口）...'
    Start-Process -FilePath $DockerDesktop | Out-Null
    for ($i = 1; $i -le 60; $i++) {
        Start-Sleep -Seconds 3
        if (Test-DockerEngine) { Ok "Docker 引擎就绪（等了 $($i * 3) 秒）"; return $true }
        if ($i % 5 -eq 0) { Say "    ...仍在等待（$($i * 3) 秒）" }
    }
    Fail 'Docker 引擎启动超时'
    Hint '手动打开 Docker Desktop，等托盘图标变绿后再跑一次本脚本'
    return $false
}

# ── 桥进程 ────────────────────────────────────────────
function Get-BridgeProcesses {
    Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*main.py*' }
}

function Stop-Bridge {
    $procs = @(Get-BridgeProcesses)
    if ($procs.Count -eq 0) { Say '    桥当前未运行'; return }
    foreach ($p in $procs) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { }
    }
    Start-Sleep -Seconds 2
    Ok "已停止桥进程（$($procs.Count) 个：$($procs.ProcessId -join ', ')）"
}

function Wait-AstrbotReady {
    param([int]$TimeoutSec = 180)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 4
        $logs = (cmd /c "docker logs --tail 300 webot-astrbot 2>&1" | Out-String)
        if ($logs -match '适配器已连接') { Ok 'AstrBot 的 OneBot 适配器已就绪'; return $true }
    }
    Warn "等 AstrBot 适配器超时（$TimeoutSec 秒）"
    Hint '看日志：docker logs --tail 100 webot-astrbot'
    return $false
}

function Start-Bridge {
    if (-not (Test-Path $Python)) {
        Fail "找不到虚拟环境 Python：$Python"
        Hint '先建虚拟环境并装依赖（见 docs/deployment.md）'
        return $false
    }

    # 微信必须在线且已登录，否则桥初始化会直接失败
    if (-not (Get-Process Weixin -ErrorAction SilentlyContinue)) {
        Warn '微信（Weixin.exe）没有运行 —— 桥需要它在线并已登录'
        if (Test-Path $WeixinExe) {
            Say '    尝试启动微信并等它加载...'
            Start-Process -FilePath $WeixinExe | Out-Null
            Start-Sleep -Seconds 15
        } else {
            Warn "找不到微信：$WeixinExe"
        }
    } else {
        Ok '微信在运行'
    }

    Say '    启动桥进程（后台，日志写 bridge\bridge.err.log）...'
    # 轮转上一次的日志：Start-Process 的重定向是**覆盖**写，不轮转的话
    # 每次重启都会把上一轮的排查证据冲掉（踩过一次：@ 失败的日志正好被重启抹了）
    foreach ($f in @($BridgeOut, $BridgeErr)) {
        if (Test-Path $f) {
            try { Move-Item -Force -Path $f -Destination ($f + '.1') -ErrorAction Stop } catch { }
        }
    }
    Start-Process -FilePath $Python -ArgumentList 'main.py' -WorkingDirectory $BridgeDir `
        -RedirectStandardOutput $BridgeOut -RedirectStandardError $BridgeErr `
        -WindowStyle Hidden | Out-Null
    return $true
}

function Wait-BridgeReady {
    param([int]$TimeoutSec = 150)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        if (-not (Test-Path $BridgeErr)) { continue }
        $tail = (Get-Content $BridgeErr -Encoding Default -Tail 80 -ErrorAction SilentlyContinue | Out-String)
        if ($tail -match '已连接 AstrBot') { Ok '桥已连上 AstrBot'; return 'connected' }
        if ($tail -match '初始化微信失败') {
            Fail '桥启动失败：微信未登录 / 未运行 / 被锁屏'
            Hint '保持微信登录、解开锁屏，然后重新跑一次'
            return 'failed'
        }
        if ($tail -match '没有可监听的群') {
            Fail '桥启动失败：config.json 里没有配置要监听的群'
            return 'failed'
        }
    }
    Warn "等桥就绪超时（$TimeoutSec 秒）"
    return 'timeout'
}

# ── 状态汇总 ──────────────────────────────────────────
function Show-Status {
    Step '状态汇总'

    if (Test-DockerEngine) {
        Ok 'Docker 引擎：运行中'
        $rows = @(docker ps --filter name=webot --format '{{.Names}} | {{.Status}}' 2>$null)
        if ($rows.Count -gt 0) {
            foreach ($r in $rows) { Say "           $r" }
        } else {
            Warn '没有 webot-* 容器在运行'
        }
    } else {
        Warn 'Docker 引擎：未运行'
    }

    $b = @(Get-BridgeProcesses)
    if ($b.Count -gt 0) { Ok "微信桥：运行中（PID $($b.ProcessId -join ', ')）" } else { Warn '微信桥：未运行' }

    if (Get-Process Weixin -ErrorAction SilentlyContinue) {
        # 账号名取自桥的日志，所以只在桥活着时才有意义
        $acct = $null
        if ($b.Count -gt 0 -and (Test-Path $BridgeErr)) {
            $line = (Get-Content $BridgeErr -Encoding Default -Tail 300 -ErrorAction SilentlyContinue |
                     Select-String '微信账号:' | Select-Object -Last 1).Line
            if ($line -match '微信账号:\s*(.+)$') { $acct = $matches[1].Trim() }
        }
        if ($acct) { Ok "微信：运行中 — $acct" } else { Ok '微信：运行中' }
    } else {
        Warn '微信：未运行（收不到也发不出消息）'
    }

    if (Get-Process ollama -ErrorAction SilentlyContinue) {
        Ok 'Ollama：运行中（知识库嵌入用）'
    } else {
        Warn 'Ollama：未运行（只影响知识库检索，不影响聊天）'
    }

    Say ''
    Say "    AstrBot 管理面板：$WebuiUrl" -ForegroundColor Cyan
    Say '    群里可用命令：/gaw（看窗口） /gactx（看注入的记忆） /gasleep（强制休眠）' -ForegroundColor DarkGray
}

# ── 动作 ──────────────────────────────────────────────
function Invoke-Start {
    Step '1/5 检查 Docker 引擎'
    if (-not (Start-DockerEngine)) { Show-Status; return 1 }

    Step '2/5 启动 AstrBot'
    Push-Location $Root
    try {
        # 为什么套一层 cmd /c：docker compose 的进度输出走 **stderr**，
        # 直接执行会被 PowerShell 当成 NativeCommandError 刷一屏红字（不影响结果，但很吓人）。
        # --no-deps：compose 早期曾 depends_on wechatpadpro（Pad 路线遗留，服务已删除）——
        # 现在没有依赖了，这个参数只是无害的保险，保留。
        cmd /c "docker compose up -d --no-deps astrbot 2>&1" |
            ForEach-Object { Say "    $($_.ToString().TrimEnd())" }
    } finally {
        Pop-Location
    }

    Step '3/5 等 AstrBot 就绪'
    [void](Wait-AstrbotReady)

    Step '4/5 拉起微信桥'
    Stop-Bridge
    if (-not (Start-Bridge)) { Show-Status; return 1 }

    Step '5/5 等桥连上'
    $r = Wait-BridgeReady

    if ($r -eq 'connected') {
        # 只看有用行：滤掉「重连中」噪音（连上之前必然有一串）
        $tail = @(Get-Content $BridgeErr -Encoding Default -Tail 40 -ErrorAction SilentlyContinue |
                  Where-Object { $_ -notmatch '连接断开/失败' } | Select-Object -Last 6)
        Show-Status
        Say ''
        Say '    --- 桥日志末尾 ---' -ForegroundColor DarkGray
        foreach ($l in $tail) { Say "    $l" -ForegroundColor DarkGray }
        Say ''
        Write-Host '全部就绪 ✔  现在可以在群里 @ 机器人了。' -ForegroundColor Green
        return 0
    }

    Show-Status
    return 1
}

function Invoke-Stop {
    Step '1/2 停止微信桥'
    Stop-Bridge

    Step '2/2 停止容器'
    Push-Location $Root
    try {
        cmd /c "docker compose stop 2>&1" | ForEach-Object { Say "    $($_.ToString().TrimEnd())" }
    } finally {
        Pop-Location
    }
    Write-Host "`n已停止（Docker Desktop 仍开着；要彻底关闭请用托盘菜单退出）。" -ForegroundColor Green
    return 0
}

function Invoke-BridgeRestart {
    # 只重启桥：改了 config.json / 桥代码 / 新进了一个群之后用。
    # 刻意不碰 Docker —— AstrBot 动它没用还慢；桥连不上时会自己每 5 秒重试。
    Step '1/3 停止当前桥进程'
    Stop-Bridge

    Step '2/3 拉起微信桥'
    if (-not (Start-Bridge)) { Show-Status; return 1 }

    Step '3/3 等桥连上'
    $r = Wait-BridgeReady

    if ($r -eq 'connected') {
        $tail = @(Get-Content $BridgeErr -Encoding Default -Tail 40 -ErrorAction SilentlyContinue |
                  Where-Object { $_ -notmatch '连接断开/失败' } | Select-Object -Last 6)
        Show-Status
        Say ''
        Say '    --- 桥日志末尾 ---' -ForegroundColor DarkGray
        foreach ($l in $tail) { Say "    $l" -ForegroundColor DarkGray }
        Say ''
        Write-Host '桥已重启 ✔  新群 / 新配置已生效，现在可以 @ 机器人了。' -ForegroundColor Green
        return 0
    }

    if ($r -eq 'timeout') {
        Hint '若 AstrBot 也没在跑，桥会一直重连直到它起来 —— 那就先跑 start.cmd'
    }
    Show-Status
    return 1
}

function Invoke-AstrRestart {
    # 只重启 AstrBot 容器：人格 / 主配置 / 插件代码改动后用。
    # 桥不需要动 —— 它连不上时会每 5 秒自动重连。
    Step '1/2 重启 AstrBot 容器'
    Push-Location $Root
    try {
        cmd /c "docker compose restart astrbot 2>&1" |
            ForEach-Object { Say "    $($_.ToString().TrimEnd())" }
    } finally {
        Pop-Location
    }

    Step '2/2 等 AstrBot 就绪'
    [void](Wait-AstrbotReady)
    Show-Status
    Say ''
    Write-Host 'AstrBot 已重启 ✔  人格 / 配置 / 插件改动已生效（桥会自动重连）。' -ForegroundColor Green
    return 0
}

# ── 入口 ──────────────────────────────────────────────
Say ''
Write-Host '==========================================' -ForegroundColor DarkCyan
Write-Host '   微信 AGI 群聊助手 · 服务管理' -ForegroundColor DarkCyan
Write-Host '==========================================' -ForegroundColor DarkCyan

switch ($Action) {
    'start' { $code = Invoke-Start }
    'stop' { $code = Invoke-Stop }
    'restart' { [void](Invoke-Stop); $code = Invoke-Start }
    'status' { Show-Status; $code = 0 }
    'bridge' { $code = Invoke-BridgeRestart }
    'astr' { $code = Invoke-AstrRestart }
}

exit $code
