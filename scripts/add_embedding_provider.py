# -*- coding: utf-8 -*-
"""把本地 Ollama 嵌入 Provider 写入 AstrBot 配置（幂等，可重复执行）。

为什么用脚本而不是手改：cmd_config.json 是 AstrBot 托管的 JSON，
直接文本替换容易破坏结构。这里用 JSON 解析 + 备份 + 原子写回。

关键点：AstrBot 跑在容器里，`localhost:11434` 指向容器自己，
必须用 `host.docker.internal:11434` 才能访问宿主机的 Ollama。

用法（**先停容器**，避免 AstrBot 回写覆盖）：
    python add_embedding_provider.py "c:\\webot\\data\\astrbot\\cmd_config.json"
"""

import json
import shutil
import sys

DEFAULT_CFG = r"c:\webot\data\astrbot\cmd_config.json"

ENTRY = {
    "provider": "ollama",
    "type": "ollama_embedding",
    "provider_type": "embedding",
    "embedding_api_base": "http://host.docker.internal:11434",
    "embedding_model": "bge-m3",
    "embedding_dimensions": 1024,
    "timeout": 120,
    "proxy": "",
    "id": "ollama_embedding",
    "enable": True,
}


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CFG
    with open(path, encoding="utf-8-sig") as f:
        cfg = json.load(f)

    srcs = cfg.setdefault("provider_sources", [])
    before = [s.get("id") for s in srcs]

    # 幂等：先移除同名条目再追加
    srcs[:] = [s for s in srcs if s.get("id") != ENTRY["id"]]
    srcs.append(dict(ENTRY))

    shutil.copy2(path, path + ".bak-embedding")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print("before:", before)
    print("after :", [s.get("id") for s in srcs])
    print("backup:", path + ".bak-embedding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
