# -*- coding: utf-8 -*-
"""通用工具：修改 AstrBot 主配置里的键（值按 JSON 解析）。

为什么用脚本：`data/astrbot/cmd_config.json` 由 AstrBot 托管，
直接手改容易破坏 JSON 结构；且**必须停容器再写**，否则会被运行中的实例回写覆盖。

用法：
    python set_astrbot_config.py kb_agentic_mode true
    python set_astrbot_config.py kb_final_top_k 5
    python set_astrbot_config.py default_kb_collection '"my_kb"'   # 字符串要带引号

完整流程：
    docker compose stop astrbot
    python set_astrbot_config.py kb_agentic_mode true
    docker compose start astrbot
"""

import io
import json
import shutil
import sys
import time
from pathlib import Path

CONFIG = Path(r"C:\webot\data\astrbot\cmd_config.json")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    key, raw = sys.argv[1], sys.argv[2]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw  # 退化为字符串

    if not CONFIG.exists():
        print(f"[x] 找不到配置文件: {CONFIG}")
        return 1

    with io.open(CONFIG, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)

    if key not in cfg:
        print(f"[!] 注意：配置里原先没有 '{key}'，将新建该键")

    before = cfg.get(key, "<不存在>")
    cfg[key] = value

    backup = CONFIG.with_name(f"{CONFIG.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CONFIG, backup)

    with io.open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print(f"[+] {key}: {before!r} -> {value!r}")
    print(f"[+] 备份 -> {backup.name}")

    # 回读校验
    with io.open(CONFIG, "r", encoding="utf-8-sig") as f:
        check = json.load(f)
    ok = check.get(key) == value
    print(f"[{'OK' if ok else 'FAIL'}] 回读校验: {check.get(key)!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
