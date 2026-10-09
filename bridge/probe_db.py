# -*- coding: utf-8 -*-
"""
探测微信数据库目录：确定 WeChatDB 的 db_dir 正确取值。

背景：若「文档」目录被 OneDrive 重定向，wechatauto-replica 的自动探测会失败。

用法：
    .\\.venv\\Scripts\\python.exe probe_db.py
    （也可用环境变量 WECHAT_DB_DIR 显式指定 xwechat_files 根目录）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CANDIDATES = [
    Path.home() / "Documents" / "xwechat_files",
    Path.home() / "OneDrive" / "文档" / "xwechat_files",
    Path.home() / "OneDrive" / "Documents" / "xwechat_files",
    Path.home() / "OneDrive" / "文件" / "xwechat_files",
]


def find_root() -> Path | None:
    env = os.environ.get("WECHAT_DB_DIR")
    if env and Path(env).exists():
        return Path(env)
    for p in CANDIDATES:
        if p.exists():
            return p
    # 兜底：遍历用户目录浅层
    for p in Path.home().glob("*/**/xwechat_files"):
        if p.is_dir():
            return p
    return None


def main() -> int:
    root = find_root()
    if root is None:
        print("[x] 未找到 xwechat_files 目录")
        return 1

    print(f"[i] xwechat_files 根目录: {root}")
    subs = [p for p in root.iterdir() if p.is_dir()]
    print(f"[i] 子目录({len(subs)}): {[p.name for p in subs]}")
    for s in subs:
        inner = [p.name for p in s.iterdir() if p.is_dir()]
        print(f"    - {s.name}: {inner[:8]}")

    from wechatauto import WeChatDB

    attempts: list[tuple[Path, dict]] = [(root, {"db_dir": str(root)})]
    for s in subs:
        attempts.append((s, {"db_dir": str(s)}))
        attempts.append((s, {"db_dir": str(s), "account": s.name}))

    for path, kw in attempts:
        try:
            db = WeChatDB(**kw)
            info = db.get_self_info()
            print(f"\n[OK] 可用参数: {kw}")
            print(f"     账号信息: {info}")
            print(f"\n>>> 请在 config.json / 代码中使用 db_dir = r\"{path}\"")
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"[..] {kw} -> {type(e).__name__}: {e}")

    print("\n[x] 所有组合都失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
