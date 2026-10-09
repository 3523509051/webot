# 虚拟显示器驱动（usbmmidd_v2 / Indirect Display Driver）管理
#
# 用途：给系统加一块**虚拟显示器**，然后把画面切到它上面（displayswitch /external），
#       物理屏就真的黑（背光也灭），而系统层面「显示器一直开着」→ **不触发连接待机**，
#       微信 / 桥 / AstrBot / AI 操控全部照跑。这是本机唯一能做到「真黑屏 + 程序继续跑」的路子。
#
# 驱动可信度（2026-10-09 已查验）：
#   * usbmmidd.cat 签名 Valid，签名者 Microsoft Windows Hardware Compatibility Publisher（WHQL）
#     → 不需要测试签名模式
#   * 与 Amyuni 官方包逐字节一致（SHA-256 比对过），NullSignal 未改动驱动
#   * 它是 UMDF 用户态间接显示驱动（IddCx），不是内核模式驱动
#
# 需要管理员的动作：install / enable / disable / remove（脚本会自己弹 UAC）。
# status 不需要管理员。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vdd.ps1 -Action status
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vdd.ps1 -Action setup    # install + enable
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vdd.ps1 -Action enable
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vdd.ps1 -Action disable
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\vdd.ps1 -Action teardown # disable + remove
#
# NOTE: keep this file UTF-8 **with BOM**（Windows PowerShell 5.1 需要），
#       改完跑一次 scripts\fix-bom.ps1。

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('status', 'setup', 'install', 'enable', 'disable', 'remove', 'teardown', 'res', 'install-task')]
    [string]$Action,
    [int]$W = 0,     # -Action res 用：目标宽
    [int]$H = 0      # -Action res 用：目标高
)

$ErrorActionPreference = 'Continue'

# ── 驱动包位置（优先官方 Amyuni 包）────────────────────────────
$pkg = $null
foreach ($c in @('c:\webot\tools\vdd\amyuni\usbmmidd_v2',
                 'c:\webot\tools\vdd\nullsignal\NullSignal-main\usbmmidd_v2')) {
    if (Test-Path (Join-Path $c 'deviceinstaller64.exe')) { $pkg = $c; break }
}
if (-not $pkg) { Write-Host '  找不到驱动包（deviceinstaller64.exe）' -ForegroundColor Red; return 1 }
$exe = Join-Path $pkg 'deviceinstaller64.exe'
$inf = (Get-ChildItem -Path $pkg -Filter '*.inf' | Select-Object -First 1)
if (-not $inf) { Write-Host '  驱动包里没有 .inf' -ForegroundColor Red; return 1 }

function Test-Admin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-Displays {
    if (-not ('DispInfo' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class DispInfo
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DISPLAY_DEVICE {
        public int cb;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
        public int StateFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DEVMODE {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
        public short dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra;
        public int dmFields;
        public int dmPositionX, dmPositionY;
        public int dmDisplayOrientation, dmDisplayFixedOutput;
        public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
        public short dmLogPixels;
        public int dmBitsPerPel, dmPelsWidth, dmPelsHeight, dmDisplayFlags, dmDisplayFrequency;
        public int dmICMMethod, dmICMIntent, dmMediaType, dmDitherType, dmReserved1, dmReserved2, dmPanningWidth, dmPanningHeight;
    }
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool EnumDisplayDevices(string lpDevice, uint iDevNum, ref DISPLAY_DEVICE lpDisplayDevice, uint dwFlags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool EnumDisplaySettings(string deviceName, int modeNum, ref DEVMODE devMode);

    public static string[] List()
    {
        var list = new List<string>();
        var d = new DISPLAY_DEVICE();
        for (uint i = 0; ; i++)
        {
            d = new DISPLAY_DEVICE();
            d.cb = Marshal.SizeOf(d);
            if (!EnumDisplayDevices(null, i, ref d, 0)) break;
            string flags = "";
            if ((d.StateFlags & 0x1) != 0) flags += "ATTACHED ";
            if ((d.StateFlags & 0x4) != 0) flags += "PRIMARY ";
            if ((d.StateFlags & 0x8) != 0) flags += "MIRROR ";
            var dm = new DEVMODE();
            dm.dmSize = (short)Marshal.SizeOf(dm);
            string mode = "";
            if (EnumDisplaySettings(d.DeviceName, -1, ref dm))
                mode = string.Format("{0}x{1}@{2}Hz", dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency);
            list.Add(string.Format("{0} | {1} | {2}| {3}", d.DeviceName, d.DeviceString, flags, mode));
        }
        return list.ToArray();
    }
}
'@
    }
    return [DispInfo]::List()
}

function Show-Displays {
    Write-Host '  === 当前显示设备 ===' -ForegroundColor DarkGray
    foreach ($l in (Get-Displays)) { Write-Host "    $l" }
}

function Show-DeviceState {
    Write-Host '  === 虚拟显示器设备状态 ===' -ForegroundColor DarkGray
    $out = (pnputil /enum-devices /deviceid usbmmidd 2>&1 | Out-String).Trim()
    if ($out -match 'USB Mobile Monitor|usbmmidd') { Write-Host "    $out" } else { Write-Host '    （未安装）' -ForegroundColor DarkGray }
}

function Invoke-Child([string[]]$argv) {
    # 保留输出：出问题要看得到（NullSignal 把输出全丢 DEVNULL，失败时是静默的，不学它）
    Push-Location $pkg
    try {
        $o = & $exe @argv 2>&1 | Out-String
        if ($o.Trim()) { Write-Host ("    " + $o.Trim()) }
        Write-Host ("    退出码: {0}" -f $LASTEXITCODE) -ForegroundColor DarkGray
        return $LASTEXITCODE
    } finally { Pop-Location }
}

# ── status：不需要管理员 ───────────────────────────────────────
if ($Action -eq 'status') {
    Write-Host ''
    Write-Host ("  驱动包 : {0}" -f $pkg) -ForegroundColor DarkGray
    Show-DeviceState
    Show-Displays
    Write-Host ''
    return 0
}

# ── 其余动作需要管理员 ─────────────────────────────────────────
if (-not (Test-Admin)) {
    Write-Host ''
    Write-Host ("  需要管理员权限（{0}），正在申请 —— 请在弹出的窗口里点「是」。" -f $Action) -ForegroundColor Yellow
    $cmdArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Action', $Action)
    # 必须把参数一起带过去 —— 否则提权后变成「只有 -Action」，-W/-H 被丢掉
    if ($W -gt 0) { $cmdArgs += @('-W', "$W") }
    if ($H -gt 0) { $cmdArgs += @('-H', "$H") }
    Start-Process powershell -Verb RunAs -ArgumentList $cmdArgs | Out-Null
    return 0
}

Write-Host ''
Write-Host ("  以管理员身份执行：{0}" -f $Action) -ForegroundColor DarkGray

# 提权后是独立窗口，宿主 shell 看不到输出 —— 转录到文件，便于事后核查
$logDir = 'c:\webot\logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logFn = Join-Path $logDir 'vdd.log'
try { Start-Transcript -Path $logFn -Append -Force | Out-Null } catch { }


switch ($Action) {
    'install' {
        Write-Host '  → 安装驱动（deviceinstaller64 install）' -ForegroundColor DarkGray
        [void](Invoke-Child @('install', $inf.Name, 'usbmmidd'))
        Show-DeviceState
    }
    'enable' {
        # ⚠️ 必须 0 → 1 循环：单发 enableidd 1 是空操作（驱动认为已启用，
        # 而显示器被 displayswitch /internal 拔掉了），实测 2026-10-09 09:57 踩到。
        Write-Host '  → 重新挂载虚拟显示器（enableidd 0 → 1 循环）' -ForegroundColor DarkGray
        [void](Invoke-Child @('enableidd', '0'))
        Start-Sleep -Seconds 3
        [void](Invoke-Child @('enableidd', '1'))
        Start-Sleep -Seconds 4
        Show-Displays
    }
    'disable' {
        Write-Host '  → 停用虚拟显示器（enableidd 0）' -ForegroundColor DarkGray
        [void](Invoke-Child @('enableidd', '0'))
        Start-Sleep -Seconds 2
        Show-Displays
    }
    'setup' {
        [void](Invoke-Child @('install', $inf.Name, 'usbmmidd'))
        [void](Invoke-Child @('enableidd', '1'))
        Start-Sleep -Seconds 3
        Show-DeviceState
        Show-Displays
    }
    'remove' {
        Write-Host '  → 卸载虚拟显示器设备与驱动包' -ForegroundColor DarkGray
        Get-PnpDevice -ErrorAction SilentlyContinue |
            Where-Object { $_.FriendlyName -like '*USB Mobile Monitor*' } |
            ForEach-Object {
                Write-Host ("    移除设备 {0}" -f $_.InstanceId) -ForegroundColor DarkGray
                pnputil /remove-device $_.InstanceId 2>&1 | Out-String | ForEach-Object { if ($_.Trim()) { Write-Host "      $($_.Trim())" } }
            }
        foreach ($p in (Get-WindowsDriver -Online -ErrorAction SilentlyContinue |
                        Where-Object { $_.ProviderName -like '*Amyuni*' -or $_.OriginalFileName -like '*usbmmidd*' })) {
            Write-Host ("    删除驱动包 {0}" -f $p.Driver) -ForegroundColor DarkGray
            pnputil /delete-driver $p.Driver /uninstall /force 2>&1 | Out-String | ForEach-Object { if ($_.Trim()) { Write-Host "      $($_.Trim())" } }
        }
    }
    'install-task' {
        # 建两个「最高权限」的计划任务，之后脚本用 schtasks 触发即可 —— **不再需要 UAC**。
        # 为什么需要：虚拟屏设备在 displayswitch /internal 之后会被置为停用状态，
        # 每次熄屏都要 enableidd 1（需管理员）。每次都弹 UAC 意味着用户不在电脑前就
        # 点不到 → 脚本静默降级成遮罩（面板照旧亮着）。2026-10-09 实测连续踩到。
        $tr = Join-Path $pkg 'deviceinstaller64.exe'
        $user = "$env:USERDOMAIN\$env:USERNAME"
        $prin = New-ScheduledTaskPrincipal -UserId $user -RunLevel Highest -LogonType S4U
        $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
        foreach ($pair in @(@('webot-vdd-enable', 'enableidd 1'), @('webot-vdd-disable', 'enableidd 0'))) {
            $act = New-ScheduledTaskAction -Execute $tr -Argument $pair[1] -WorkingDirectory $pkg
            Register-ScheduledTask -TaskName $pair[0] -Action $act -Principal $prin -Settings $set -Force | Out-Null
            Write-Host ("  已注册计划任务 {0}（{1}）" -f $pair[0], $pair[1]) -ForegroundColor Green
        }

        # 网易 GameViewer（UU 远程）的守护服务会周期性地把显示配置改回去 ——
        # 实测熄屏约 16 秒后内屏被拉回（2026-10-09 10:01:22）。熄屏期间临时停掉它。
        $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $gvStop = '-NoProfile -WindowStyle Hidden -Command "Stop-Service -Name GameViewerService -Force -ErrorAction SilentlyContinue; Get-Process GameViewer* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"'
        $gvStart = '-NoProfile -WindowStyle Hidden -Command "Start-Service -Name GameViewerService -ErrorAction SilentlyContinue"'
        foreach ($gv in @(@('webot-gv-stop', $gvStop), @('webot-gv-start', $gvStart))) {
            $act = New-ScheduledTaskAction -Execute $ps -Argument $gv[1]
            Register-ScheduledTask -TaskName $gv[0] -Action $act -Principal $prin -Settings $set -Force | Out-Null
            Write-Host ("  已注册计划任务 {0}" -f $gv[0]) -ForegroundColor Green
        }
        Write-Host '  之后脚本触发它们不再需要 UAC。' -ForegroundColor DarkGray
    }
    'res' {
        if ($W -le 0 -or $H -le 0) { Write-Host '  需要 -W 与 -H' -ForegroundColor Yellow; return 1 }

        # 驱动支持的分辨率来自它自己的模式列表（默认内置 9 档，最高 3840x2160，不含 2736x1824）。
        # INF 里那段 AddReg（HKR, "Parameters\Monitors"）本该把它落到设备硬件键，
        # 但 deviceinstaller 装完后本机并没有这个键 —— 所以我们自己创建并把目标分辨率写成默认值，
        # 再重启虚拟设备让它重新读取。
        $dev = Get-PnpDevice -Class Display -ErrorAction SilentlyContinue |
               Where-Object { $_.FriendlyName -like '*USB Mobile Monitor*' } | Select-Object -First 1
        if (-not $dev) { Write-Host '  没找到虚拟显示器设备，先 -Action setup' -ForegroundColor Yellow; return 1 }
        $inst = $dev.InstanceId
        Write-Host ("  设备实例: {0}" -f $inst) -ForegroundColor DarkGray

        $key = "HKLM\SYSTEM\CurrentControlSet\Enum\$inst\Parameters\Monitors"
        Write-Host ("  → 写入模式列表默认值 {0},{1}" -f $W, $H) -ForegroundColor DarkGray
        $r1 = (reg add $key /ve /t REG_SZ /d "$W,$H" /f 2>&1 | Out-String).Trim()
        Write-Host "    $r1"
        # 同时塞进编号项（驱动可能同时枚举默认值与编号值）
        foreach ($idx in 0..9) {
            $cur = (reg query $key /v $idx 2>&1 | Out-String)
            if ($cur -notmatch 'REG_SZ\s+(\S+)') {
                $r2 = (reg add $key /v $idx /t REG_SZ /d "$W,$H" /f 2>&1 | Out-String).Trim()
                Write-Host ("    编号 {0}: {1}" -f $idx, $r2)
                break
            }
        }

        Write-Host '  → 重启虚拟设备（enableidd 0/1）以重新读取模式列表' -ForegroundColor DarkGray
        [void](Invoke-Child @('enableidd', '0'))
        Start-Sleep -Seconds 2
        [void](Invoke-Child @('enableidd', '1'))
        Start-Sleep -Seconds 3

        Write-Host '  → 应用分辨率' -ForegroundColor DarkGray
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot '_vdd_res.ps1') -Mode set -W $W -H $H
        Show-Displays
    }
    'teardown' {
        [void](Invoke-Child @('enableidd', '0'))
        Start-Sleep -Seconds 2
        Get-PnpDevice -ErrorAction SilentlyContinue |
            Where-Object { $_.FriendlyName -like '*USB Mobile Monitor*' } |
            ForEach-Object { pnputil /remove-device $_.InstanceId 2>&1 | Out-Null }
        foreach ($p in (Get-WindowsDriver -Online -ErrorAction SilentlyContinue |
                        Where-Object { $_.ProviderName -like '*Amyuni*' -or $_.OriginalFileName -like '*usbmmidd*' })) {
            pnputil /delete-driver $p.Driver /uninstall /force 2>&1 | Out-Null
        }
        Show-Displays
    }
}

Write-Host ''
Write-Host '  完成。' -ForegroundColor Green
try { Stop-Transcript | Out-Null } catch { }
Write-Host ''
