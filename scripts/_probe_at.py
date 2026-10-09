# -*- coding: utf-8 -*-
"""黑屏下反复测「机器人真 @」：连发 3 条带 @ 的消息，打印每次结果。

用途：定位「关屏后 @ 不可用」——是每次都失败，还是偶发。
"""
import sys
import time

sys.path.insert(0, r"c:\webot\bridge")

from wechat_client import WeChatClient  # noqa: E402

c = WeChatClient(bot_nickname="大肥鱼")
print("init ok", flush=True)

for i in range(1, 4):
    t = time.time()
    try:
        r = c.send_at("测试群", "小泽布尔", f"【黑屏@测试{i}】", "忽略这条 🐟")
    except Exception as e:  # noqa: BLE001
        r = f"异常: {e}"
    print(f"第 {i} 次 @小泽布尔 -> {r}   {time.time() - t:.1f}s", flush=True)
    time.sleep(2)
