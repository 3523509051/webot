# -*- coding: utf-8 -*-
"""
向 AstrBot 主配置写入 OneBot v11 (aiocqhttp) 适配器。

用途：Windows 微信桥通过「反向 WebSocket」连接到 AstrBot，
      AstrBot 侧需要先存在一个 aiocqhttp 类型的消息平台。

用法（先停 AstrBot 容器再执行，避免被运行中的配置覆盖）：
    python scripts/patch_astrbot_config.py
"""

import io
import json
import shutil
import sys
import time
from pathlib import Path

CONFIG = Path(r"C:\webot\data\astrbot\cmd_config.json")

PLATFORM_NAME = "OneBot v11"
PLATFORM_CONFIG = {
    "id": "default",
    "type": "aiocqhttp",
    "enable": True,
    "ws_reverse_host": "0.0.0.0",
    "ws_reverse_port": 6199,
    "ws_reverse_token": "",
}


def main() -> int:
    if not CONFIG.exists():
        print(f"[x] 找不到配置文件: {CONFIG}")
        return 1

    backup = CONFIG.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CONFIG, backup)
    print(f"[+] 已备份 -> {backup.name}")

    with io.open(CONFIG, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)

    before = cfg.get("platform")
    print(f"[i] 原有平台: {before}")

    # AstrBot 的 platform 是「列表」，每个元素是一个平台实例
    cfg["platform"] = [dict(PLATFORM_CONFIG)]

    with io.open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print(f"[+] 已写入平台: {PLATFORM_NAME} -> {PLATFORM_CONFIG}")

    # 回读校验
    with io.open(CONFIG, "r", encoding="utf-8-sig") as f:
        check = json.load(f)
    print(f"[OK] 回读校验通过: {check['platform']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
