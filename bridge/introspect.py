# -*- coding: utf-8 -*-
"""
自检脚本：确认 wechatauto-replica 能读到微信数据，并打印消息字段结构。

前置：微信已登录并保持运行。
用法：
    .\\.venv\\Scripts\\python.exe introspect.py
"""
from __future__ import annotations

import json
import sys
import traceback


def dump(title: str, value, limit: int = 3):
    print(f"\n=== {title} ===")
    if isinstance(value, list):
        print(f"(list, len={len(value)})")
        for item in value[:limit]:
            print(json.dumps(item, ensure_ascii=False, default=str)[:800])
    else:
        print(json.dumps(value, ensure_ascii=False, default=str)[:1500])


def call(obj, name, *args, **kwargs):
    """兼容「方法」与「属性」两种形式。"""
    attr = getattr(obj, name, None)
    if attr is None:
        return None, f"no attr {name}"
    if callable(attr):
        try:
            return attr(*args, **kwargs), None
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"
    return attr, None


def main() -> int:
    try:
        from wechatauto import WeChatDB
    except Exception as e:  # noqa: BLE001
        print(f"[x] 导入 wechatauto 失败: {e}")
        return 1

    from paths import find_xwechat_root

    root = find_xwechat_root()
    print(f"[i] 微信数据目录: {root}")
    try:
        db = WeChatDB(db_dir=str(root)) if root else WeChatDB()
    except Exception as e:  # noqa: BLE001
        print(f"[x] 初始化 WeChatDB 失败（微信是否已登录并运行？）: {e}")
        traceback.print_exc()
        return 1

    print("[+] WeChatDB 初始化成功")

    info, err = call(db, "get_self_info")
    print(f"self_info: {info}  (err={err})")

    groups, err = call(db, "get_groups")
    print(f"get_groups err={err}")
    dump("groups", groups)

    sessions, err = call(db, "get_sessions", 10)
    print(f"\nget_sessions err={err}")
    dump("sessions", sessions)

    # 找一个群来验证消息结构
    gid = None
    if isinstance(groups, list) and groups:
        g = groups[0]
        gid = g.get("username") or g.get("wxid") or g.get("id") if isinstance(g, dict) else None
    if gid is None and isinstance(sessions, list):
        for s in sessions:
            if isinstance(s, dict) and str(s.get("username", "")).endswith("@chatroom"):
                gid = s["username"]
                break

    if gid:
        msgs, err = call(db, "get_messages", gid, 5, 0)
        print(f"\nget_messages({gid}) err={err}")
        dump("messages", msgs, limit=5)
        if isinstance(msgs, list) and msgs:
            print("\n单条消息的全部字段名:", list(msgs[0].keys()))
    else:
        print("\n[!] 未找到群会话，无法验证消息结构")

    # 昵称映射
    nm, err = call(db, "nickname_map")
    print(f"\nnickname_map err={err}")
    if isinstance(nm, dict):
        print(f"(dict, len={len(nm)}) 样例:", list(nm.items())[:3])

    for attr in ("wxid", "account", "account_dir"):
        v, e = call(db, attr)
        print(f"{attr}: {v} (err={e})")

    # GUI 可用性（只实例化，不发送）
    try:
        from wechatauto.guia import WeChatGUI
        gui = WeChatGUI()
        print("\n[+] WeChatGUI 初始化成功")
        for m in ("calibrate_layout",):
            r, e = call(gui, m, save=True)
            print(f"  {m} -> {r} (err={e})")
    except Exception as e:  # noqa: BLE001
        print(f"\n[x] WeChatGUI 初始化失败: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
