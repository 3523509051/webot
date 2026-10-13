# -*- coding: utf-8 -*-
"""
定位微信 4.x 数据目录（兼容「文档」被 OneDrive 重定向的情况）。

wechatauto-replica 的自动探测只看标准 Documents 路径，
若用户的「文档」被重定向到 OneDrive，就需要显式传入 db_dir。
"""
from __future__ import annotations

import os
import json
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
    # 自检与桥共用本机配置；config.json 已被 Git 忽略，不硬编码机器地址。
    config_path = Path(__file__).with_name("config.json")
    if config_path.exists():
        with config_path.open(encoding="utf-8-sig") as f:
            config = json.load(f)
        configured = (config.get("wechat") or {}).get("db_dir")
        if configured:
            configured_path = Path(configured).expanduser()
            if not configured_path.is_absolute():
                configured_path = config_path.parent / configured_path
            if not configured_path.is_dir():
                raise FileNotFoundError(f"wechat.db_dir 配置的目录不存在：{configured_path}")
            return configured_path
    for p in CANDIDATES:
        if p.exists():
            return p
    # 兜底：浅层遍历用户目录
    for p in Path.home().glob("*/**/xwechat_files"):
        if p.is_dir():
            return p
    return None
