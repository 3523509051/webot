# -*- coding: utf-8 -*-
"""
探测消息字段结构：读取若干会话的最近消息，打印全部字段。

用法：
    .\\.venv\\Scripts\\python.exe probe_msgs.py [会话username ...]
"""
from __future__ import annotations

import json
import sys

from paths import find_xwechat_root
from wechatauto import WeChatDB


def main() -> int:
    targets = sys.argv[1:] or ["filehelper", "newsapp"]

    root = find_xwechat_root()
    db = WeChatDB(db_dir=str(root)) if root else WeChatDB()
    print(f"[i] db_dir = {root}")
    print(f"[i] 账号 = {db.get_self_info()}\n")

    # 会话列表（找群）
    sessions = db.get_sessions(limit=30) or []
    rooms = [s for s in sessions if str(s.get("username", "")).endswith("@chatroom")]
    print(f"[i] 群会话({len(rooms)}): {[s['username'] for s in rooms]}")

    for user in targets:
        print(f"\n{'='*60}\n>>> 会话: {user}")
        try:
            msgs = db.get_messages(user, 5, 0)
        except Exception as e:  # noqa: BLE001
            print(f"  get_messages ERR: {type(e).__name__}: {e}")
            continue
        if not msgs:
            print("  (无消息)")
            continue
        print(f"  共 {len(msgs)} 条；字段名: {list(msgs[0].keys())}")
        for m in msgs[:5]:
            print("  -", json.dumps(m, ensure_ascii=False, default=str)[:400])

        # 增量接口
        try:
            seq = msgs[0].get("sort_seq", 0)
            inc = db.get_new_messages(user, seq)
            print(f"  get_new_messages(since={seq}) -> {len(inc or [])} 条")
        except Exception as e:  # noqa: BLE001
            print(f"  get_new_messages ERR: {type(e).__name__}: {e}")

    # 群名映射
    try:
        print(f"\n[i] group_id_to_name = {db.group_id_to_name()}")
    except Exception as e:  # noqa: BLE001
        print(f"\n[i] group_id_to_name ERR: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
