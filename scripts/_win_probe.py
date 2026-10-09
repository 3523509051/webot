# -*- coding: utf-8 -*-
"""列出所有顶层窗口：标题 / 类名 / 进程，找出「投影」等功能窗口的归属。"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys

import uiautomation as auto

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def proc_name(pid: int) -> str:
    if not pid:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.split("\\")[-1]
    finally:
        kernel32.CloseHandle(h)
    return ""


rows = []
for w in auto.GetRootControl().GetChildren():
    try:
        name = w.Name or ""
        cls = w.ClassName or ""
        pid = w.ProcessId or 0
        rect = w.BoundingRectangle
        vis = not w.IsOffscreen
        if not vis:
            continue
        rows.append((pid, proc_name(pid), cls, name, rect))
    except Exception:
        continue

print(f"{'PID':>7}  {'进程':<28} {'类名':<40} 标题")
print("-" * 130)
for pid, pname, cls, name, rect in sorted(rows):
    print(f"{pid:>7}  {pname:<28} {cls[:40]:<40} {name!r}  {rect}")
