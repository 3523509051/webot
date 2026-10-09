# webot — 熄屏但保持运行
#
# **唯一方案：虚拟屏**（把显示拓扑切到虚拟显示器 usbmmidd_v2，微软 WHQL 签名）。
#   物理屏失去信号 → 面板断电、背光灭；而系统层面「显示器一直开着」
#   → **不触发连接待机**，微信 / 桥 / AstrBot / AI 操控全部照跑。
#   2026-10-09 实测通过：物理屏黑着期间无任何待机事件，桥照常发出带 @ 的消息。
#
# 弃用过的路线（**不要再用**，留档见 docs/deployment.md §4.3《弃用过的三条路》）：
#   · 调背光 —— 面板仍发光，不算黑屏
#   · 真·面板断电（DPMS / 电源键=关闭显示器）—— 本机只支持 S0 待机，而 S0 由「显示屏关闭」
#     触发 → 整机进连接待机：进程被 DAM 挂起、网卡断开，表现为熄屏期间不回消息
#   · 禁用 Modern Standby —— 试过且一直没生效，虚拟屏路线也不需要它（相关脚本已删除）
# 代码里保留了 `-Mode overlay|dpms` 两个分支，**仅供排障**，挂机不要用。
#
# 恢复：**真人**碰一下鼠标或键盘。机器人自己动不会误唤醒 —— 用低级钩子过滤 INJECTED
# 合成输入，已实测（scripts\_probe_hook.ps1）：SetCursorPos 不产生事件、mouse_event 带 injected=True。
#
# 用法：
#   screen-off.cmd               （双击：体检 → 切虚拟屏，物理屏真黑）
#   screen-off.ps1 -Restore      （立刻恢复：切回内屏）
#   screen-off.ps1 -NoOff        （只体检，不熄屏）
#   screen-off.ps1 -Seconds 20   （测试：20 秒后自动恢复）
#
# 依赖：scripts\vdd.ps1（安装/启用驱动与免 UAC 计划任务，需管理员）。
#       驱动包缺失或切换失败时，本脚本**明确报错退出**，不会静默变成别的方案。
#
# NOTE: keep this file UTF-8 **with BOM**（Windows PowerShell 5.1 需要），
#       改完跑一次 scripts\fix-bom.ps1。

param(
    [switch]$NoOff,                                          # 只体检
    [switch]$Restore,                                        # 立刻恢复
    [ValidateSet('auto', 'virtual', 'overlay', 'dpms')]
    [string]$Mode = 'auto',
    [int]$Seconds = 0,                                       # >0：N 秒后自动恢复（测试用）
    [int]$VddW = 3840,                                       # 虚拟屏目标分辨率
    [int]$VddH = 2160
)

$ErrorActionPreference = 'Continue'
$logDir = 'c:\webot\logs'
$logFn  = Join-Path $logDir 'screen-off.log'
$stateFn = Join-Path $logDir 'screen-off.state'
$stopFn  = Join-Path $logDir 'screen-off.stop'   # 让正在跑的熄屏实例退出的标志
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Enable-Vdd {
    # 重新挂载虚拟显示器。**必须做 0 → 1 的循环**：
    # 实测（2026-10-09 09:57/09:58）单发 `enableidd 1` 是空操作（驱动认为已启用，
    # 而显示器早被 displayswitch /internal 拔掉了），只有先 0 再 1 才会重新插上。
    # 优先走**计划任务**（`vdd.ps1 -Action install-task` 建的，以最高权限运行）——
    # 触发它们**不需要 UAC**，人在不在电脑前都能挂上。
    $hasTask = Get-ScheduledTask -TaskName 'webot-vdd-disable' -ErrorAction SilentlyContinue
    if ($hasTask) {
        Start-ScheduledTask -TaskName 'webot-vdd-disable' -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        Start-ScheduledTask -TaskName 'webot-vdd-enable' -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 6
        return
    }
    Write-Host '  需要管理员启用虚拟屏 —— 会弹 UAC，请点「是」' -ForegroundColor Yellow
    Write-Host '    想以后免 UAC：跑 scripts\vdd.ps1 -Action install-task（需管理员，一次性）' -ForegroundColor DarkGray
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'vdd.ps1') -Action enable
    Start-Sleep -Seconds 6
}

function Write-Log([string]$msg) {
    # 日志写入绝不能影响熄屏本身：两个实例并发写时 Add-Content 可能报
    # "Stream was not readable"，所以包一层
    try {
        Add-Content -Path $logFn -Value ('{0}  {1}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $msg) -Encoding UTF8
    } catch { }
    Write-Host "  $msg" -ForegroundColor DarkGray
}

# ── C#：真人输入监听（低级钩子，过滤注入事件）+ 显示模式控制 ──────
$csSource = @'
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

public static class WebotBlackout
{
    const int WH_KEYBOARD_LL = 13;
    const int WH_MOUSE_LL = 14;
    const int LLKHF_INJECTED = 0x10;
    const int LLKHF_LOWER_IL_INJECTED = 0x02;
    const int LLMHF_INJECTED = 0x01;
    const int WM_CLOSE = 0x0010;
    const int WS_EX_TRANSPARENT = 0x00000020;
    const int WS_EX_TOOLWINDOW = 0x00000080;
    const int WS_EX_LAYERED = 0x00080000;
    const int WS_EX_NOACTIVATE = 0x08000000;

    public const string Title = "webot-blackout";

    delegate IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true)]
    static extern IntPtr SetWindowsHookEx(int idHook, HookProc lpfn, IntPtr hMod, uint dwThreadId);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hhk);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);
    [DllImport("kernel32.dll", CharSet = CharSet.Auto)] static extern IntPtr GetModuleHandle(string lpModuleName);
    [DllImport("user32.dll", CharSet = CharSet.Auto)] static extern IntPtr FindWindow(string lpClassName, string lpWindowName);
    [DllImport("user32.dll")] static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);

    static volatile bool _human = false;
    static volatile bool _closed = false;
    static int _injectedCount = 0;
    static HookProc _kbdProc, _mouseProc;
    static IntPtr _hk = IntPtr.Zero, _hm = IntPtr.Zero;
    static BlackForm _form;

    // 被过滤掉的注入事件数（机器人自己点的）：打印出来以证明「没被误唤醒」
    public static int InjectedCount { get { return _injectedCount; } }

    class BlackForm : Form
    {
        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams cp = base.CreateParams;
                cp.ExStyle |= WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE;
                return cp;
            }
        }
        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            _closed = true;
            base.OnFormClosing(e);
        }
    }

    static IntPtr KbdHook(int code, IntPtr wp, IntPtr lp)
    {
        if (code >= 0)
        {
            int flags = Marshal.ReadInt32(lp, 8);   // KBDLLHOOKSTRUCT.flags
            if ((flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)) != 0) _injectedCount++;
            else _human = true;
        }
        return CallNextHookEx(_hk, code, wp, lp);
    }

    static IntPtr MouseHook(int code, IntPtr wp, IntPtr lp)
    {
        if (code >= 0)
        {
            int flags = Marshal.ReadInt32(lp, 12);  // MSLLHOOKSTRUCT.flags
            if ((flags & LLMHF_INJECTED) != 0) _injectedCount++;
            else _human = true;
        }
        return CallNextHookEx(_hm, code, wp, lp);
    }

    static void InstallHooks()
    {
        IntPtr mod = GetModuleHandle(null);
        _kbdProc = KbdHook; _mouseProc = MouseHook;
        _hk = SetWindowsHookEx(WH_KEYBOARD_LL, _kbdProc, mod, 0);
        _hm = SetWindowsHookEx(WH_MOUSE_LL, _mouseProc, mod, 0);
    }

    static void RemoveHooks()
    {
        if (_hk != IntPtr.Zero) { UnhookWindowsHookEx(_hk); _hk = IntPtr.Zero; }
        if (_hm != IntPtr.Zero) { UnhookWindowsHookEx(_hm); _hm = IntPtr.Zero; }
    }

    // 只等真人输入，不显示任何窗口（虚拟屏模式用）
    public static string WaitHuman(int maxSeconds)
    {
        BeginWatch();
        DateTime deadline = DateTime.UtcNow.AddSeconds(maxSeconds);
        while (!_human && DateTime.UtcNow < deadline)
        {
            Application.DoEvents();      // 泵消息，钩子回调才会被派发
            Thread.Sleep(40);
        }
        EndWatch();
        return _human ? "input" : "timeout";
    }

    // ── 常驻监听：钩子装上后不卸载，由 PowerShell 侧循环做轻量检查 ──
    // 为什么需要：之前是「每 2 秒调一次 WaitHuman」，两次之间钩子是卸载的，
    // 而看门狗检查要 spawn 一个新 PowerShell（1~3 秒）—— 那段时间真人输入会被漏掉。
    public static void BeginWatch()
    {
        _human = false; _injectedCount = 0;
        InstallHooks();
    }

    public static bool HumanSeen { get { return _human; } }

    public static void EndWatch()
    {
        RemoveHooks();
    }

    // 全黑遮罩 + 等真人输入（备选模式用）
    public static string Run(int maxSeconds)
    {
        _human = false; _closed = false; _injectedCount = 0;
        IntPtr prevFg = GetForegroundWindow();

        _form = new BlackForm();
        _form.FormBorderStyle = FormBorderStyle.None;
        _form.StartPosition = FormStartPosition.Manual;
        _form.Bounds = SystemInformation.VirtualScreen;   // 多显示器全覆盖
        _form.BackColor = Color.Black;
        _form.TopMost = true;
        _form.ShowInTaskbar = false;
        _form.Text = Title;
        _form.Show();
        if (prevFg != IntPtr.Zero) SetForegroundWindow(prevFg);  // 不抢前台

        InstallHooks();
        DateTime deadline = DateTime.UtcNow.AddSeconds(maxSeconds);
        while (!_human && !_closed && DateTime.UtcNow < deadline)
        {
            Application.DoEvents();
            Thread.Sleep(40);
        }
        string reason = _human ? "input" : (_closed ? "closed" : "timeout");
        RemoveHooks();
        try { _form.Close(); _form.Dispose(); } catch { }
        _form = null;
        if (prevFg != IntPtr.Zero) SetForegroundWindow(prevFg);
        return reason;
    }

    public static bool CloseExisting()
    {
        IntPtr h = FindWindow(null, Title);
        if (h == IntPtr.Zero) return false;
        PostMessage(h, WM_CLOSE, IntPtr.Zero, IntPtr.Zero);
        return true;
    }
}

// 虚拟显示器的查找与分辨率设置
public static class DispMode
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
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int ChangeDisplaySettingsEx(string deviceName, ref DEVMODE devMode, IntPtr hwnd, uint flags, IntPtr lParam);

    const int ENUM_CURRENT_SETTINGS = -1;
    const uint CDS_UPDATEREGISTRY = 0x00000001;

    public static string FindVirtual(int requireAttached)
    {
        var d = new DISPLAY_DEVICE();
        for (uint i = 0; ; i++)
        {
            d = new DISPLAY_DEVICE();
            d.cb = Marshal.SizeOf(d);
            if (!EnumDisplayDevices(null, i, ref d, 0)) break;
            if (d.DeviceString != null && d.DeviceString.IndexOf("USB Mobile Monitor", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                if (requireAttached == 0 || (d.StateFlags & 0x1) != 0) return d.DeviceName;
            }
        }
        return null;
    }

    public static string Current(string dev)
    {
        var dm = new DEVMODE(); dm.dmSize = (short)Marshal.SizeOf(dm);
        if (EnumDisplaySettings(dev, ENUM_CURRENT_SETTINGS, ref dm))
            return string.Format("{0}x{1}", dm.dmPelsWidth, dm.dmPelsHeight);
        return "?";
    }

    // 有没有「非虚拟」的显示器又被接上了（= 真屏亮回来了）
    public static bool AnyNonVirtualAttached()
    {
        var d = new DISPLAY_DEVICE();
        for (uint i = 0; ; i++)
        {
            d = new DISPLAY_DEVICE();
            d.cb = Marshal.SizeOf(d);
            if (!EnumDisplayDevices(null, i, ref d, 0)) break;
            bool attached = (d.StateFlags & 0x1) != 0;
            bool virt = d.DeviceString != null &&
                        d.DeviceString.IndexOf("USB Mobile Monitor", StringComparison.OrdinalIgnoreCase) >= 0;
            if (attached && !virt) return true;
        }
        return false;
    }

    // 0 = 成功；-999 = 模式列表里没有该分辨率
    public static int SetRes(string dev, int w, int h)
    {
        var dm = new DEVMODE();
        for (int i = 0; ; i++)
        {
            dm = new DEVMODE();
            dm.dmSize = (short)Marshal.SizeOf(dm);
            if (!EnumDisplaySettings(dev, i, ref dm)) break;
            if (dm.dmPelsWidth == w && dm.dmPelsHeight == h)
                return ChangeDisplaySettingsEx(dev, ref dm, IntPtr.Zero, CDS_UPDATEREGISTRY, IntPtr.Zero);
        }
        return -999;
    }
}
'@
Add-Type -TypeDefinition $csSource -ReferencedAssemblies System.Windows.Forms, System.Drawing

# ── 已经在熄屏？双击第二次 / -Restore = 恢复 ─────────────────────
if ($Restore -or -not $NoOff) {
$did = $false
if (Test-Path $stateFn) {
    $st = Get-Content $stateFn -Raw | ConvertFrom-Json
    if ($st.mode -eq 'virtual') {
        Write-Host ''
        Write-Host '  检测到处于虚拟屏熄屏状态，正在切回内屏…' -ForegroundColor Yellow
        # 先让正在跑的那个实例退出，免得它的看门狗又把屏幕切黑
        Set-Content -Path $stopFn -Value 'stop' -Encoding UTF8
        Start-Sleep -Seconds 5
        Remove-Item $stopFn -Force -ErrorAction SilentlyContinue
        & displayswitch.exe /internal | Out-Null
        Start-Sleep -Seconds 3
        $did = $true
        Write-Log '已恢复（虚拟屏 → 内屏）'
    }
    Remove-Item $stateFn -Force -ErrorAction SilentlyContinue
}
    if ([WebotBlackout]::CloseExisting()) {
        Write-Host '  检测到全黑遮罩，已请求关闭。' -ForegroundColor Yellow
        Write-Log '已恢复（关闭遮罩）'
        $did = $true
    }
    if ($did -and $Restore) { return }
    if (-not $did -and $Restore) {
        Write-Host '  当前没有处于熄屏状态。' -ForegroundColor DarkGray
        return
    }
}

# ── 体检 ───────────────────────────────────────────────────
Write-Host ''
Write-Host '  熄屏前体检 --------------------------------' -ForegroundColor DarkGray
$bridge = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*main.py*' })
if ($bridge.Count -gt 0) {
    Write-Host ("  桥        : 运行中（{0} 个进程）" -f $bridge.Count) -ForegroundColor Green
} else {
    Write-Host '  桥        : 没在跑 ✗   先双击 start.cmd' -ForegroundColor Yellow
}
$astrbot = ''
try { $astrbot = (docker ps --filter 'name=webot-astrbot' --format '{{.Status}}' 2>$null | Select-Object -First 1) } catch { }
if ($astrbot) {
    Write-Host ("  AstrBot   : {0}" -f $astrbot) -ForegroundColor Green
} else {
    Write-Host '  AstrBot   : 容器没在跑 ✗   先双击 start.cmd' -ForegroundColor Yellow
}

# ── 选哪种熄屏方式 ─────────────────────────────────────────
# ⚠️ 不要用「探测驱动在不在」来决定走哪条路：实测 EnumDisplayDevices 与 Get-PnpDevice
# 在某些时刻都会返回空（/internal 之后、以及后台进程里都遇到过），据此判断会误降级成遮罩
# （2026-10-09 09:48 / 09:50 / 09:52 连续踩到）。改成「先假定能用虚拟屏，真起不来再降级」——
# 虚拟屏分支里已经带了自愈（重新 enableidd）和降级链。
$vddOn = [DispMode]::FindVirtual(1)       # 当前是否有活跃虚拟屏（仅用于提示）

$powerTxt = (powercfg /a 2>&1) -join "`n"
$yesSection = ''
if ($powerTxt -match '(?s)此系统上有以下睡眠状态:(.*?)此系统上没有以下睡眠状态') { $yesSection = $Matches[1] }
$s0Avail = $yesSection -match 'S0 低电量待机'

$chosen = $Mode
if ($Mode -eq 'auto') { $chosen = 'virtual' }   # 总是先试虚拟屏；起不来会自动降级
if ($chosen -eq 'virtual' -and
    -not (Test-Path 'c:\webot\tools\vdd\amyuni\usbmmidd_v2\deviceinstaller64.exe')) {
    # 驱动包缺失 → **明确报错退出**（不降级：虚拟屏是唯一方案，静默换成别的会让人以为已熄屏）
    Write-Host '  ✗ 找不到虚拟显示器驱动包，无法熄屏。' -ForegroundColor Red
    Write-Host '    先装驱动：scripts\vdd.ps1 -Action setup（需管理员，一次即可）' -ForegroundColor Yellow
    Write-Host '    驱动包位置：tools\vdd\amyuni\usbmmidd_v2\' -ForegroundColor DarkGray
    Write-Log '✗ 缺虚拟显示器驱动包，未熄屏（虚拟屏是唯一方案）'
    exit 2
}
if ($chosen -eq 'dpms' -and $s0Avail) {
    Write-Host '  ⚠️ dpms：本机仍支持 S0 待机，面板断电会触发连接待机（进程被挂起、断网）' -ForegroundColor Yellow
}

$modeName = switch ($chosen) {
    'virtual' { '虚拟屏（面板真黑，程序照跑）' }
    'overlay' { '全黑遮罩（视觉黑，面板仍供电）' }
    'dpms'    { '真·面板断电（DPMS）' }
}
Write-Host ("  熄屏方式  : {0}" -f $modeName) -ForegroundColor Green
Write-Host '  睡眠/休眠 : 从不（AC/DC）｜合盖不动作｜电源键/睡眠键=不操作（防误触进待机）' -ForegroundColor Green
Write-Host '  ------------------------------------------' -ForegroundColor DarkGray

if ($NoOff) {
    Write-Host '  (-NoOff：只体检，不熄屏)' -ForegroundColor DarkGray
    Write-Host ''
    return
}

if ($bridge.Count -eq 0 -or -not $astrbot) {
    Write-Host ''
    Write-Host '  警告：有服务没在跑，熄屏后你不会看到任何提示。' -ForegroundColor Yellow
    Write-Host '        仍会继续（8 秒后）—— 想先修就按 Ctrl+C。' -ForegroundColor Yellow
    Start-Sleep -Seconds 8
} elseif ($Seconds -le 0) {
    Write-Host ''
    Write-Host '  5 秒后熄屏（碰一下鼠标或键盘即恢复）' -ForegroundColor DarkGray
    Start-Sleep -Seconds 5
}

$maxSec = 600 * 60                          # 保险：最多 10 小时，超时自动恢复
if ($Seconds -gt 0) { $maxSec = $Seconds }

# ── ① 虚拟屏：真黑 + 程序照跑 ───────────────────────────────
# ⚠️ 关键认知（2026-10-09 用 _vdd_step.ps1 逐步验证得出）：
#   初始时虚拟屏是**未挂载**状态，而 **`displayswitch /external` 这个动作本身就会把它挂载起来**
#   （DISPLAY14 → 3840x2160，成为主屏），`enableidd` 循环在没有切换动作时是空操作。
#   之前的写法要求"虚拟屏先挂载、再切换"，于是永远判定失败 → 静默降级成遮罩
#   （这正是用户看到的"根本没有黑屏"）。所以：**先切，再检查**。
if ($chosen -eq 'virtual') {
    @{ mode = 'virtual'; started = (Get-Date).ToString('s') } |
        ConvertTo-Json | Set-Content -Path $stateFn -Encoding UTF8
    Write-Log '虚拟屏熄屏开始（直接 /external，虚拟屏由该动作自动挂载）'

    & displayswitch.exe /external | Out-Null
    Start-Sleep -Seconds 4

    # ⚠️ 关键一步（2026-10-09 实测根因）：DisplaySwitch.exe **不会自己退出**，
    # 它留着全屏 Shell_LightDismissOverlay +「投影」面板；面板约 15 秒后自动消失时
    # **会把显示配置回退**（三次复现：10:01/10:03/10:05，熄屏 15~40 秒后内屏自己亮回来）。
    # 杀掉它之后拓扑就钉住了（_vdd_test5 实测 55 秒不反弹）。
    Stop-Process -Name DisplaySwitch -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    $now = [DispMode]::FindVirtual(1)

    if (-not $now) {
        # 少见情况：切换后仍没挂上 —— 用计划任务 cycle 一次再切
        Write-Log '  切换后仍未挂载虚拟屏 —— cycle 一次后再切'
        Enable-Vdd
        & displayswitch.exe /external | Out-Null
        Start-Sleep -Seconds 5
        $now = [DispMode]::FindVirtual(1)
    }

    if ((-not $now) -or [DispMode]::AnyNonVirtualAttached()) {
        # 内屏还在 = 没真正熄掉 → 切回内屏并**明确失败**（不降级：虚拟屏是唯一方案）
        Write-Log '  ✗ 切换后内屏仍在（或虚拟屏没挂上）—— 切回内屏，本次未熄屏'
        & displayswitch.exe /internal | Out-Null
        Start-Sleep -Seconds 3
        Remove-Item $stateFn -Force -ErrorAction SilentlyContinue
        Write-Host '  ✗ 未能熄灭内屏，本次不熄屏。排查：' -ForegroundColor Red
        Write-Host '    1) 驱动是否装好：scripts\vdd.ps1 -Action status' -ForegroundColor Yellow
        Write-Host '    2) 两个免 UAC 计划任务是否在：webot-vdd-enable / webot-vdd-disable' -ForegroundColor Yellow
        Write-Host '    3) 详细过程：logs\screen-off.log 与 scripts\_vdd_diag.ps1' -ForegroundColor Yellow
        exit 3
    } else {
        # 切过去后 Windows 会用驱动默认模式（1024x768），微信窗口会被挤出屏幕、
        # 输入框不可达 → @ 必然失败。所以这里必须把分辨率顶上去（实测踩到）。
        $r = [DispMode]::SetRes($now, $VddW, $VddH)
        Write-Log ("  虚拟屏 {0} → {1}x{2}（返回 {3}）；内屏已摘除" -f $now, $VddW, $VddH, $r)
    }
}

if ($chosen -eq 'virtual') {

    # 等待真人输入；同时当看门狗 —— 实测有东西会把内屏"拉回来"（本机跑着网易
    # GameViewerService/Server/Healthd，这类服务会自己维持显示配置）。
    # 一旦发现内屏又被接上（= 亮屏），立刻再切一次，并把每次发生都记进日志（这就是证据）。
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $reason = 'timeout'
    $reassert = 0
    # 钩子**常驻**（不每 2 秒拆装），循环里 DoEvents 泵消息 —— 真人输入不会漏检；
    # 看门狗改用进程内的显示枚举（毫秒级），不再 spawn PowerShell。
    [WebotBlackout]::BeginWatch()
    try {
        while ($true) {
            [System.Windows.Forms.Application]::DoEvents()
            if ([WebotBlackout]::HumanSeen) { $reason = 'input'; break }
            if ($sw.Elapsed.TotalSeconds -ge $maxSec) { $reason = 'timeout'; break }
            # 外部要求停止（-Restore / 再次双击）—— 否则两个实例会互相打架：
            # 一个切回内屏，另一个看门狗立刻又切黑
            if (Test-Path $stopFn) {
                Remove-Item $stopFn -Force -ErrorAction SilentlyContinue
                $reason = 'closed'
                break
            }

            $vddNow = [DispMode]::FindVirtual(1)
            $internal = [DispMode]::AnyNonVirtualAttached()
            if ((-not $vddNow) -or $internal) {
                $reassert++
                Write-Log ("  ⚠️ 内屏被拉回（虚拟屏在={0} 内屏在={1}）—— 第 {2} 次重新切到虚拟屏" -f `
                    [bool]$vddNow, $internal, $reassert)
                & displayswitch.exe /external | Out-Null
                Start-Sleep -Seconds 3
                Stop-Process -Name DisplaySwitch -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 1
                $now = [DispMode]::FindVirtual(1)
                if ($now) { [void][DispMode]::SetRes($now, $VddW, $VddH) }
            }
            Start-Sleep -Milliseconds 250
        }
    } finally {
        [WebotBlackout]::EndWatch()
    }
    if ($reassert) { Write-Log ("  看门狗共重新切换 {0} 次" -f $reassert) }

    $why = if ($reason -eq 'input') { '真人输入（鼠标/键盘）' }
           elseif ($reason -eq 'closed') { '被恢复命令 / 再次双击停止' }
           elseif ($maxSec -lt 60) { "超过 $maxSec 秒自动恢复" }
           else { ("超过 {0} 分钟自动恢复" -f [int][Math]::Round($maxSec / 60.0)) }

    & displayswitch.exe /internal | Out-Null
    Start-Sleep -Seconds 3
    # 同样杀掉它，避免它稍后又把"恢复"给回退掉
    Stop-Process -Name DisplaySwitch -Force -ErrorAction SilentlyContinue
    Remove-Item $stateFn -Force -ErrorAction SilentlyContinue
    Write-Log ("虚拟屏熄屏结束：{0}；期间过滤掉机器人注入事件 {1} 个" -f $why, [WebotBlackout]::InjectedCount)
    Write-Host ''
    Write-Host ("  已恢复内屏：{0}" -f $why) -ForegroundColor Green
    Write-Host '  程序全程运行，日志见 c:\webot\logs\screen-off.log' -ForegroundColor DarkGray
    return
}

# ── ③ 真·面板断电（DPMS）────────────────────────────────────
if ($chosen -eq 'dpms') {
    Add-Type -Namespace Win32 -Name Display -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true)]
public static extern IntPtr SendMessageTimeout(
    IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam,
    uint fuFlags, uint uTimeout, out IntPtr lpdwResult);
'@
    $WM_SYSCOMMAND = 0x0112; $SC_MONITORPOWER = 0xF170
    $HWND_BROADCAST = [IntPtr]0xffff; $MONITOR_OFF = [IntPtr]2
    $r = [IntPtr]::Zero
    [void][Win32.Display]::SendMessageTimeout($HWND_BROADCAST, $WM_SYSCOMMAND,
        [IntPtr]$SC_MONITORPOWER, $MONITOR_OFF, 2, 2000, [ref]$r)
    Write-Log '面板断电（SC_MONITORPOWER off）'
    Write-Host '  显示器已断电 —— 动一下鼠标/键盘即恢复。' -ForegroundColor Green
    return
}

# ── ② 全黑遮罩（备选）───────────────────────────────────────
Write-Log ("黑屏遮罩开始（{0}x{1}）" -f `
    [System.Windows.Forms.SystemInformation]::VirtualScreen.Width, `
    [System.Windows.Forms.SystemInformation]::VirtualScreen.Height)
$r = [WebotBlackout]::Run($maxSec)
$why = switch ($r) {
    'input'   { '真人输入（鼠标/键盘）' }
    'closed'  { '被关闭（-Restore 或再次双击）' }
    'timeout' {
        if ($maxSec -lt 60) { "超过 $maxSec 秒自动恢复" }
        else { ("超过 {0} 分钟自动恢复" -f [int][Math]::Round($maxSec / 60.0)) }
    }
    default   { $r }
}
Write-Log ("黑屏遮罩结束：{0}；期间过滤掉机器人注入事件 {1} 个（没有误唤醒）" -f `
    $why, [WebotBlackout]::InjectedCount)
Write-Host ''
Write-Host ("  已恢复：{0}" -f $why) -ForegroundColor Green
Write-Host '  程序全程运行，日志见 c:\webot\logs\screen-off.log' -ForegroundColor DarkGray
