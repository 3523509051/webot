# -*- coding: utf-8 -*-
"""黑屏遮罩下，桥用的 UIA 路径还能不能定位并点击微信？

只做无害操作：把微信置前 → 定位聊天输入框 → SetFocus → 在输入框中心点一下。
不输入任何文字、不发送任何消息。
"""
import sys
import time

sys.path.insert(0, r"c:\webot\bridge")

t0 = time.time()
from wechatauto.guia import WeChatGUI  # noqa: E402

print("import 耗时 %.1fs" % (time.time() - t0))

g = WeChatGUI()
uia = g._get_uia()
print("ensure_window:", uia.ensure_window())

win = getattr(uia, "_win", None)
print("win:", win)

edit = uia._chat_input(win)
print("edit:", edit)
print("edit.Exists:", edit.Exists(2))
print("edit.IsOffscreen:", edit.IsOffscreen)

edit.SetFocus()
time.sleep(0.2)
t1 = time.time()
edit.Click()          # 无害：点输入框中心，只把焦点放进去
print("click 耗时 %.2fs" % (time.time() - t1))
print("OK")
