# 显示拓扑控制：切到虚拟屏（虚拟屏变主屏 + 把真屏断开）／恢复真屏
#
# 为什么不用 displayswitch.exe：实测（2026-10-09，10:01 / 10:03 / 10:05 三次复现）
#   * 它切完**自己不退出**，还留着一个全屏 Shell_LightDismissOverlay（窗口名「关闭」）+「投影」面板；
#   * 大约 15~40 秒后 Windows 会把配置**回退**（DISPLAY1 又 ATTACHED、虚拟屏消失）——
#     这正是"熄屏期间屏幕自己亮回来"的原因（疑似"保留显示设置"回退路径）。
# 这里改用 ChangeDisplaySettingsEx 的持久化做法：
#   1) 把虚拟屏 CDS_SET_PRIMARY 设为主显示器
#   2) 把内屏用 NULL DEVMODE 置为「断开」（写注册表，持久）
# 恢复时按切走前保存的模式把内屏重新接回，并还原主显示器。

if (-not ('DispCtl' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class DispCtl
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

    const int ENUM_CURRENT_SETTINGS = -1;
    const uint CDS_UPDATEREGISTRY = 0x00000001;
    const uint CDS_SET_PRIMARY = 0x00000010;
    const uint ATTACHED = 0x1;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool EnumDisplayDevices(string dev, uint n, ref DISPLAY_DEVICE dd, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern bool EnumDisplaySettings(string dev, int n, ref DEVMODE dm);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int ChangeDisplaySettingsEx(string dev, ref DEVMODE dm, IntPtr hwnd, uint flags, IntPtr param);
    // 同一个入口点的另一种调用形态：dm 传 NULL = 断开该显示器
    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "ChangeDisplaySettingsExW")]
    static extern int ChangeDisplaySettingsExNull(string dev, IntPtr dm, IntPtr hwnd, uint flags, IntPtr param);

    static bool IsVirtual(string s) {
        return s != null && s.IndexOf("USB Mobile Monitor", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    public static string[] Snap() {
        var list = new List<string>();
        var d = new DISPLAY_DEVICE();
        for (uint i = 0; ; i++) {
            d = new DISPLAY_DEVICE();
            d.cb = Marshal.SizeOf(d);
            if (!EnumDisplayDevices(null, i, ref d, 0)) break;
            bool attached = (d.StateFlags & ATTACHED) != 0;
            if (!attached) continue;
            var dm = new DEVMODE(); dm.dmSize = (short)Marshal.SizeOf(dm);
            string mode = "?";
            if (EnumDisplaySettings(d.DeviceName, ENUM_CURRENT_SETTINGS, ref dm))
                mode = string.Format("{0}x{1}", dm.dmPelsWidth, dm.dmPelsHeight);
            list.Add(string.Format("{0}|{1}|{2}|{3}", d.DeviceName, d.DeviceString, mode,
                ((d.StateFlags & 0x4) != 0) ? "PRIMARY" : ""));
        }
        return list.ToArray();
    }

    public static string VirtualAttached() {
        var d = new DISPLAY_DEVICE();
        for (uint i = 0; ; i++) {
            d = new DISPLAY_DEVICE();
            d.cb = Marshal.SizeOf(d);
            if (!EnumDisplayDevices(null, i, ref d, 0)) break;
            if (IsVirtual(d.DeviceString) && (d.StateFlags & ATTACHED) != 0) return d.DeviceName;
        }
        return null;
    }

    public static string InternalAttached() {
        var d = new DISPLAY_DEVICE();
        for (uint i = 0; ; i++) {
            d = new DISPLAY_DEVICE();
            d.cb = Marshal.SizeOf(d);
            if (!EnumDisplayDevices(null, i, ref d, 0)) break;
            if (!IsVirtual(d.DeviceString) && (d.StateFlags & ATTACHED) != 0) return d.DeviceName;
        }
        return null;
    }

    public static string Mode(string dev) {
        var dm = new DEVMODE(); dm.dmSize = (short)Marshal.SizeOf(dm);
        if (EnumDisplaySettings(dev, ENUM_CURRENT_SETTINGS, ref dm))
            return string.Format("{0},{1}", dm.dmPelsWidth, dm.dmPelsHeight);
        return "";
    }

    public static int SetRes(string dev, int w, int h) {
        var dm = new DEVMODE();
        for (int i = 0; ; i++) {
            dm = new DEVMODE();
            dm.dmSize = (short)Marshal.SizeOf(dm);
            if (!EnumDisplaySettings(dev, i, ref dm)) break;
            if (dm.dmPelsWidth == w && dm.dmPelsHeight == h)
                return ChangeDisplaySettingsEx(dev, ref dm, IntPtr.Zero, CDS_UPDATEREGISTRY, IntPtr.Zero);
        }
        return -999;
    }

    public static int SetPrimary(string dev) {
        var dm = new DEVMODE(); dm.dmSize = (short)Marshal.SizeOf(dm);
        if (!EnumDisplaySettings(dev, ENUM_CURRENT_SETTINGS, ref dm)) return -1;
        return ChangeDisplaySettingsEx(dev, ref dm, IntPtr.Zero, CDS_SET_PRIMARY | CDS_UPDATEREGISTRY, IntPtr.Zero);
    }

    public static int Disable(string dev) {
        return ChangeDisplaySettingsExNull(dev, IntPtr.Zero, IntPtr.Zero, CDS_UPDATEREGISTRY, IntPtr.Zero);
    }

    public static int EnableWith(string dev, int w, int h) {
        return SetRes(dev, w, h);
    }
}
'@
}

# 把桌面切到虚拟屏（返回内屏原模式字符串，供恢复用；失败返回 $null）
function Switch-ToVirtual([int]$W = 3840, [int]$H = 2160) {
    $internal = [DispCtl]::InternalAttached()
    $virtual = [DispCtl]::VirtualAttached()
    if (-not $virtual) { Write-Warning '当前没有挂载的虚拟显示器'; return $null }

    $savedMode = ''
    if ($internal) { $savedMode = [DispCtl]::Mode($internal) }

    $r1 = [DispCtl]::SetRes($virtual, $W, $H)
    $r2 = [DispCtl]::SetPrimary($virtual)
    $r3 = if ($internal) { [DispCtl]::Disable($internal) } else { 0 }
    Write-Host ("    SetRes={0} SetPrimary={1} DisableInternal={2}（内屏原模式 {3}）" -f $r1, $r2, $r3, $savedMode) -ForegroundColor DarkGray
    return @{ mode = $savedMode; internal = $internal; virtual = $virtual }
}

function Restore-Internal($state) {
    if (-not $state) { return }
    if ($state.internal) {
        $wh = $state.mode -split ','
        if ($wh.Count -eq 2) {
            $r = [DispCtl]::SetRes($state.internal, [int]$wh[0], [int]$wh[1])
            Write-Host ("    内屏重新接回 {0} → {1}" -f $state.mode, $r) -ForegroundColor DarkGray
            [void][DispCtl]::SetPrimary($state.internal)
        }
    }
}
