# -*- coding: utf-8 -*-
"""
定位微信 4.x 数据目录（兼容「文档」被 OneDrive 重定向的情况）。

wechatauto-replica 的自动探测只看标准 Documents 路径，
若用户的「文档」被重定向到 OneDrive，就需要显式传入 db_dir。
"""
from __future__ import annotations

import os
from pathlib import Path

CANDIDATES = [
    Path.home() / "Documents" / "xwechat_files",
    Path.home() / "OneDrive" / "文档" / "xwechat_files",
    Path.home() / "OneDrive" / "Documents" / "xwechat_files",
    Path.home() / "OneDrive" / "文件" / "xwechat_files",
]


def find_xwechat_root() -> Path | None:
    """返回 xwechat_files 根目录；找不到返回 None。"""
    env = os.environ.get("WECHAT_DB_DIR")
    if env and Path(env).exists():
        return Path(env)
    for p in CANDIDATES:
        if p.exists():
            return p
    # 兜底：浅层遍历用户目录
    for p in Path.home().glob("*/**/xwechat_files"):
        if p.is_dir():
            return p
    return None
