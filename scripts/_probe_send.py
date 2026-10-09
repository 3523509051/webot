# -*- coding: utf-8 -*-
"""虚拟屏实测：用桥自己的代码往「测试群」发一条带 @ 的消息。

只在测试群发，正文明确标注是自动化测试。目的是验证：
  1) 显示拓扑切到虚拟屏后，UIA 定位/点击/@ 弹层/粘贴/回车 是否仍然可用
  2) 切群路径（若当前会话不是测试群）会不会因为窗口几何变化而走错
"""
import sys
import time

sys.path.insert(0, r"c:\webot\bridge")

t0 = time.time()
from wechat_client import WeChatClient  # noqa: E402

print("import %.1fs" % (time.time() - t0))

t1 = time.time()
c = WeChatClient(bot_nickname="大肥鱼")
print("client init %.1fs" % (time.time() - t1))

try:
    print("groups:", [g.get("name") for g in c.groups()][:8])
except Exception as e:  # noqa: BLE001
    print("groups 读取失败:", e)

try:
    gid = c.group_id("测试群")
    names = sorted(c.member_names(gid))[:12] if gid else []
    print("测试群 id:", gid, "成员样例:", names)
except Exception as e:  # noqa: BLE001
    print("成员读取失败:", e)

try:
    uia = c.gui._get_uia()
    print("current_chat:", uia.current_chat() if uia else None)
except Exception as e:  # noqa: BLE001
    print("current_chat 读取失败:", e)

t2 = time.time()
ok = c.send_at(
    "测试群",
    "小泽布尔",
    "【虚拟屏测试】",
    "忽略这条，正在验证「物理屏黑掉时发送链路是否正常」🐟",
)
print("send_at ->", ok, " 用时 %.1fs" % (time.time() - t2))
