# 只读探测：低级鼠标钩子能否区分「真人移动」与「程序注入」。
# 为什么需要：熄屏后要「真人一动就亮、机器人自己动不亮」。
# 不点任何鼠标键（只做移动类事件），不改任何设置。

$src = @'
using System;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

public static class HookProbe
{
    const int WH_MOUSE_LL = 14;
    delegate IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll", SetLastError = true)]
    static extern IntPtr SetWindowsHookEx(int idHook, HookProc lpfn, IntPtr hMod, uint dwThreadId);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);
    [DllImport("kernel32.dll", CharSet = CharSet.Auto)] static extern IntPtr GetModuleHandle(string n);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] static extern void mouse_event(uint f, int dx, int dy, int d, IntPtr extra);
    public struct POINT { public int X; public int Y; }

    static HookProc _p;
    public static string Log = "";

    static IntPtr Handler(int code, IntPtr wp, IntPtr lp)
    {
        if (code >= 0)
        {
            int flags = Marshal.ReadInt32(lp, 12);
            IntPtr extra = Marshal.ReadIntPtr(lp, 16);
            Log += string.Format("msg=0x{0:X} injected={1} extra=0x{2:X}\r\n",
                wp.ToInt64(), (flags & 1) != 0, extra.ToInt64());
        }
        return CallNextHookEx(IntPtr.Zero, code, wp, lp);
    }

    public static void Install() { _p = Handler; SetWindowsHookEx(WH_MOUSE_LL, _p, GetModuleHandle(null), 0); }

    public static void Pump(int ms)
    {
        var end = DateTime.UtcNow.AddMilliseconds(ms);
        while (DateTime.UtcNow < end) { Application.DoEvents(); Thread.Sleep(20); }
    }

    public static void Jump(int dx, int dy)
    {
        POINT p; GetCursorPos(out p);
        SetCursorPos(p.X + dx, p.Y + dy);
        Pump(150);
        SetCursorPos(p.X, p.Y);
    }

    public static void InjectMove()
    {
        POINT p; GetCursorPos(out p);
        // MOUSEEVENTF_MOVE|ABSOLUTE：只移动，不点击
        mouse_event(0x0001 | 0x8000, p.X * 65535 / 1920, p.Y * 65535 / 1080, 0, IntPtr.Zero);
    }
}
'@
Add-Type -TypeDefinition $src -ReferencedAssemblies System.Windows.Forms

[HookProbe]::Install()
[HookProbe]::Pump(300)

'--- A) 基线：1.5 秒不动鼠标（应为空；有内容说明你手在动鼠标）---'
[HookProbe]::Log = ''
[HookProbe]::Pump(1500)
if ([HookProbe]::Log) { [HookProbe]::Log } else { '  （无事件 = 钩子工作正常）' }

'--- B) SetCursorPos（机器人点击用的正是它）---'
[HookProbe]::Log = ''
[HookProbe]::Jump(3, 3)
if ([HookProbe]::Log) { [HookProbe]::Log } else { '  （无事件：不会误唤醒 ✓）' }

'--- C) mouse_event 注入式移动 ---'
[HookProbe]::Log = ''
[HookProbe]::InjectMove()
[HookProbe]::Pump(400)
if ([HookProbe]::Log) { [HookProbe]::Log } else { '  （无事件）' }
