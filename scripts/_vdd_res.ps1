# 探测/设置「虚拟显示器」的分辨率（测试用辅助脚本，后续并入 screen-off.ps1）
#   powershell -File scripts\_vdd_res.ps1 -Mode list
#   powershell -File scripts\_vdd_res.ps1 -Mode set -W 1920 -H 1080
param(
    [ValidateSet('list', 'set')][string]$Mode = 'list',
    [int]$W = 0,
    [int]$H = 0
)

$cs = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

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

    public const int ENUM_CURRENT_SETTINGS = -1;
    public const uint CDS_UPDATEREGISTRY = 0x00000001;

    // 找到「USB Mobile Monitor」那块已连接的显示器的设备名，如 \\.\DISPLAY15
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
                bool attached = (d.StateFlags & 0x1) != 0;
                if (requireAttached == 0 || attached) return d.DeviceName;
            }
        }
        return null;
    }

    public static string[] ListModes(string dev)
    {
        var list = new List<string>();
        var seen = new HashSet<string>();
        var dm = new DEVMODE();
        for (int i = 0; ; i++)
        {
            dm = new DEVMODE();
            dm.dmSize = (short)Marshal.SizeOf(dm);
            if (!EnumDisplaySettings(dev, i, ref dm)) break;
            string s = string.Format("{0}x{1}@{2}", dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency);
            if (seen.Add(s)) list.Add(s);
        }
        return list.ToArray();
    }

    public static string Current(string dev)
    {
        var dm = new DEVMODE(); dm.dmSize = (short)Marshal.SizeOf(dm);
        if (EnumDisplaySettings(dev, ENUM_CURRENT_SETTINGS, ref dm))
            return string.Format("{0}x{1}@{2}", dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency);
        return "?";
    }

    // 返回 0 = 成功（DISP_CHANGE_SUCCESSFUL）；-999 = 该分辨率不在模式列表里
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
Add-Type -TypeDefinition $cs

$dev = [DispMode]::FindVirtual(1)
if (-not $dev) {
    Write-Host '  没找到已连接的虚拟显示器（USB Mobile Monitor）' -ForegroundColor Yellow
    Write-Host '  先跑： scripts\vdd.ps1 -Action enable' -ForegroundColor DarkGray
    return 1
}
Write-Host ("  虚拟显示器: {0}  当前 {1}" -f $dev, [DispMode]::Current($dev)) -ForegroundColor Green

if ($Mode -eq 'list') {
    Write-Host '  === 支持的模式 ===' -ForegroundColor DarkGray
    foreach ($m in [DispMode]::ListModes($dev)) { Write-Host "    $m" }
    return 0
}

if ($W -le 0 -or $H -le 0) { Write-Host '  需要 -W 与 -H' -ForegroundColor Yellow; return 1 }
$r = [DispMode]::SetRes($dev, $W, $H)
$meaning = switch ($r) {
    0      { 'DISP_CHANGE_SUCCESSFUL' }
    -1     { 'DISP_CHANGE_FAILED' }
    -2     { 'DISP_CHANGE_BADMODE（该设备不支持这个模式）' }
    -3     { 'DISP_CHANGE_NOTUPDATED' }
    -4     { 'DISP_CHANGE_BADFLAGS' }
    -5     { 'DISP_CHANGE_BADPARAM' }
    -6     { 'DISP_CHANGE_BADDUALVIEW' }
    -999   { '模式列表里没有这个分辨率' }
    default { "未知（$r）" }
}
Write-Host ("  设置 {0}x{1} → {2}（{3}）" -f $W, $H, $r, $meaning) -ForegroundColor Green
Start-Sleep -Seconds 2
Write-Host ("  现在: {0}" -f [DispMode]::Current($dev))
