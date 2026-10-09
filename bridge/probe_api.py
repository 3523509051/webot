# -*- coding: utf-8 -*-
"""打印 WeChatDB 的完整 API 签名，用于校准调用方式。"""
from __future__ import annotations

import inspect
import json

from paths import find_xwechat_root
from wechatauto import WeChatDB


def main() -> int:
    root = find_xwechat_root()
    db = WeChatDB(db_dir=str(root)) if root else WeChatDB()

    print("=== WeChatDB 公开成员 ===")
    for name in sorted(dir(db)):
        if name.startswith("_"):
            continue
        attr = getattr(db, name, None)
        if callable(attr):
            try:
                print(f"  {name}{inspect.signature(attr)}")
            except Exception as e:  # noqa: BLE001
                print(f"  {name}() [签名不可得: {e}]")
        elif isinstance(attr, (dict, list, str, int, float, bool, type(None))):
            print(f"  {name} = {str(attr)[:100]}")

    print("\n=== list_message_chats() ===")
    try:
        chats = db.list_message_chats()
        print(json.dumps(chats, ensure_ascii=False, default=str)[:2000])
    except Exception as e:  # noqa: BLE001
        print(f"  ERR: {type(e).__name__}: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
