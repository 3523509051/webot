# -*- coding: utf-8 -*-
"""黑屏自测：用桥的生产代码依次验证三条发送路径。

① 纯文本 → 当前群（direct，验证粘贴+回车）
② @ 成员 → 当前群（验证 @ 弹层 + 精确选人 + 粘贴）
③ 纯文本 → 另一个群（**要切群**，验证 OCR + 布局校准 + 点击）
全程只往测试群/MC🚢 发明确标注为自测的文字。
"""
import sys
import time

sys.path.insert(0, r"c:\webot\bridge")

from wechat_client import WeChatClient  # noqa: E402

t0 = time.time()
c = WeChatClient(bot_nickname="大肥鱼")
print(f"client init {time.time() - t0:.1f}s", flush=True)

names = [g.get("name") for g in c.groups()]
print("groups:", names, flush=True)
other = next((n for n in names if n and n != "测试群"), None)
print("切群目标:", other, flush=True)


def run(label, fn):
    t = time.time()
    try:
        r = fn()
    except Exception as e:  # noqa: BLE001
        r = f"异常: {e}"
    print(f"{label} -> {r}   {time.time() - t:.1f}s", flush=True)


run("① 纯文本 → 测试群", lambda: c.send_text("测试群", "【黑屏自测·纯文本】忽略这条 🐟"))
time.sleep(1)
run("② @小泽布尔 → 测试群", lambda: c.send_at("测试群", "小泽布尔", "【黑屏自测·@】", "忽略这条 🐟"))
time.sleep(1)
if other:
    run(f"③ 纯文本 → {other}（切群）", lambda: c.send_text(other, "【黑屏自测·切群】忽略这条 🐟"))

try:
    uia = c.gui._get_uia()
    print("current_chat:", uia.current_chat() if uia else None, flush=True)
except Exception as e:  # noqa: BLE001
    print("current_chat 读取失败:", e, flush=True)
