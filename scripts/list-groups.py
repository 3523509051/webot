# -*- coding: utf-8 -*-
"""列出微信账号所在的全部群，用于填 bridge/config.json 的 wechat.groups。

只读数据库，不驱动界面、不发消息 —— 桥在运行时也能安全执行。

用法：
    c:\\webot\\bridge\\.venv\\Scripts\\python.exe scripts\\list-groups.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))

from paths import find_xwechat_root  # noqa: E402
from wechatauto import WeChatDB  # noqa: E402


def main() -> int:
    cfg_path = ROOT / "bridge" / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        watching = cfg.get("wechat", {}).get("groups") or {}
    except Exception as e:  # noqa: BLE001
        print(f"[!] 读取 {cfg_path} 失败: {e}")
        watching = {}

    db = WeChatDB(db_dir=str(find_xwechat_root()))
    groups = db.get_groups() or []

    print(f"微信账号所在的群：共 {len(groups)} 个")
    print()
    print(f"  {'':<3} {'群 ID':<34} {'界面显示名'}")
    print(f"  {'-' * 3} {'-' * 34} {'-' * 24}")
    for g in groups:
        if not isinstance(g, dict):
            continue
        gid = g.get("username") or g.get("wxid") or g.get("id") or ""
        name = g.get("nick_name") or g.get("nickname") or g.get("remark") or ""
        if not name:
            try:
                name = db.group_id_to_name(gid) or ""
            except Exception:  # noqa: BLE001
                name = ""
        mark = "[已在监听]" if gid in watching else ""
        print(f"  {mark:<3} {gid:<34} {name}")

    print()
    print("要新增监听的群，把它的「群 ID → 界面显示名」填进 bridge/config.json 的")
    print("wechat.groups，然后双击 start.cmd 重启即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
