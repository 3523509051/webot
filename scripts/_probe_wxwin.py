# -*- coding: utf-8 -*-
"""列出 weixin.exe 的所有顶层窗口：类名/标题/矩形/可见/最小化/UIA 是否可见。
用于诊断「加了虚拟显示器后，微信主窗口找不到了」。
"""
import ctypes
import ctypes.wintypes as wt
import subprocess

import uiautomation as auto

user32 = ctypes.windll.user32

print("=== tasklist weixin.exe ===")
out = subprocess.run(
    ["tasklist", "/FI", "IMAGENAME eq weixin.exe", "/FO", "CSV"],
    capture_output=True, text=True, encoding="gbk", errors="replace",
).stdout
print(out.strip())

pids = set()
for line in out.splitlines()[1:]:
    parts = [p.strip('"') for p in line.split('","')]
    if len(parts) >= 2 and parts[1].isdigit():
        pids.add(int(parts[1]))
print("pids:", pids)

print()
print("=== 顶层窗口（按 pid 过滤）===")
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _list_win32():
    rows = []

    def cb(hwnd, lparam):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            rows.append({
                "hwnd": hwnd,
                "cls": cls.value,
                "title": title.value,
                "rect": (r.left, r.top, r.right, r.bottom),
                "w": r.right - r.left,
                "h": r.bottom - r.top,
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "minimized": bool(user32.IsIconic(hwnd)),
            })
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return rows


for row in _list_win32():
    print("{hwnd:#x} cls={cls!r} title={title!r} rect={rect} {w}x{h} "
          "visible={visible} minimized={minimized}".format(**row))

print()
print("=== UIA 视角（根的子窗口里 pid 匹配的）===")
root = auto.GetRootControl()
for w in root.GetChildren():
    try:
        if w.ProcessId in pids:
            print(repr(w.Name), "|", w.ClassName, "|", w.BoundingRectangle,
                  "| offscreen=", w.IsOffscreen)
    except Exception as e:  # noqa: BLE001
        print("err:", e)

print()
ctrl = [w for w in root.GetChildren()
        if getattr(w, "ClassName", "") and "MainWindow" in str(getattr(w, "ClassName", ""))]
print("=== 含 MainWindow 的类名窗口 ===")
for w in ctrl:
    try:
        print(repr(w.Name), "|", w.ClassName, "|", w.BoundingRectangle, "| offscreen=", w.IsOffscreen)
    except Exception as e:  # noqa: BLE001
        print("err:", e)
