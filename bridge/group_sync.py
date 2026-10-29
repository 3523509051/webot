"""桥的插件：每次启动把微信群列表回传到本机 betty-server（POST /api/admin/groups/sync）。

- 只用标准库，不依赖微信 UI；在后台线程里执行，失败只记日志，不影响桥启动。
- 不使用系统代理（Tailscale 直连）。
- 令牌来自 config.json 的 group_sync.token（与本机 .env 的 BETTY_GUI_TOKEN 相同）。
- OneBot 群 ID = crc32(wxid) % 10^9，与 main.py 的 _fake_id 一致。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import zlib

log = logging.getLogger("group_sync")


def onebot_group_id(wxid: str) -> int:
    return zlib.crc32(wxid.encode("utf-8")) % (10 ** 9)


def push_groups(cfg: dict, groups: list, monitored_ids: set, bot_name: str) -> bool:
    gs = (cfg or {}).get("group_sync") or {}
    if not gs.get("enabled") or not gs.get("url"):
        log.info("[群同步] 未启用，跳过回传")
        return False
    items = []
    for g in groups:
        wxid = str(g.get("id") or "")
        if not wxid.endswith("@chatroom"):
            continue
        items.append({
            "name": str(g.get("name") or ""),
            "wxid": wxid,
            "onebot_id": onebot_group_id(wxid),
            "monitored": wxid in monitored_ids,
        })
    payload = {"bridge_ts": int(time.time()), "bot_name": bot_name, "groups": items}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        gs["url"], data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "X-Admin-Token": str(gs.get("token") or "")},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as resp:
            log.info("[群同步] 已回传 %d 个群到本机（HTTP %s）", len(items), resp.status)
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("[群同步] 回传失败（本机 betty-server 未运行或网络不通）: %s", e)
        return False
