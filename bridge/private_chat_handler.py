# -*- coding: utf-8 -*-
"""Bounded private control protocol. Never log message text or activation codes."""
from __future__ import annotations

import collections
import logging
import re
import time
import zlib
from typing import NamedTuple

log = logging.getLogger("bridge.private")


def private_user_id(wxid: str) -> int:
    return zlib.crc32(wxid.encode("utf-8")) % (10 ** 9)


class SyncResult(NamedTuple):
    """授权名单同步结果：`ok` 区分"确实撤销"与"暂时无法验证"。

    - `ok=True, grants={...}`：成功，空字典表示**真实撤销**（应当停止读取与投递）。
    - `ok=False`：网络/服务异常或未配置令牌，名单**未知**，调用方必须暂停投递并
      保留队列与水位，等下一次成功同步再按最新权限继续。
    """

    ok: bool
    grants: dict[str, dict]
    reason: str = "ok"


async def sync_authorized_privates(pero_url: str, internal_token: str,
                                   account_id: str) -> SyncResult:
    """Fail closed with an explicit status.

    每个数值目标都必须同时匹配账号与真实 wxid（`private_user_id`），冲突项直接丢弃。
    失败**不返回空名单**，避免调用方把"查不到"误当成"全部撤销"。
    """
    if not internal_token:
        return SyncResult(False, {}, "no_token")
    # aiohttp 只在真正发请求时导入：本模块的纯逻辑（格式判定/限流表）可在
    # 没有 aiohttp 的环境里被离线验收脚本直接复用。
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client:
            async with client.get(pero_url.rstrip("/") + "/internal/v1/private/authorized",
                                  headers={"Authorization": "Bearer " + internal_token}) as resp:
                resp.raise_for_status()
                data = await resp.json()
    except Exception as exc:
        log.warning("Private authorization sync failed (%s)", type(exc).__name__)
        return SyncResult(False, {}, "network_error")
    # 顶层必须是对象且带 `privates` 列表：`{}` / `privates=None` / 顶层数组一律
    # 视为**不可验证**。否则"格式错误"会被当成"成功空名单"而撤销全部授权。
    if not isinstance(data, dict):
        log.warning("Private authorization sync failed (invalid_response:not_object)")
        return SyncResult(False, {}, "invalid_response")
    items = data.get("privates")
    if not isinstance(items, list):
        log.warning("Private authorization sync failed (invalid_response:privates)")
        return SyncResult(False, {}, "invalid_response")
    result = {}
    collisions = set()
    rejected = 0
    try:
        for item in items:
            if not isinstance(item, dict):
                rejected += 1
                continue
            if str(item.get("account_id")) != str(account_id):
                rejected += 1
                continue
            wxid = str(item.get("user_wxid") or "")
            ref = str(item.get("user_ref") or "")
            if not wxid or wxid.endswith("@chatroom") or ref != str(private_user_id(wxid)):
                rejected += 1
                continue
            if ref in result and result[ref]["user_wxid"] != wxid:
                collisions.add(ref)
            result[ref] = item
    except Exception as exc:  # noqa: BLE001 - 单条异常不能让整个响应变成"空名单"
        log.warning("Private authorization sync failed (invalid_response:%s)",
                    type(exc).__name__)
        return SyncResult(False, {}, "invalid_response")
    if collisions or rejected:
        # 受控记录：只有数量，不含 wxid/正文。
        log.warning(
            "Private authorization sync: 忽略无效条目 %d 个、冲突 %d 个",
            rejected, len(collisions),
        )
    return SyncResult(
        True,
        {ref: item for ref, item in result.items() if ref not in collisions},
        "ok",
    )


async def redeem_activation_code(pero_url: str, internal_token: str, channel_id: str,
                                 account_id: str, user_ref: str, user_wxid: str,
                                 activation_code: str, event_id: str,
                                 display_name: str = "") -> dict:
    if not internal_token or not user_wxid or user_ref != str(private_user_id(user_wxid)):
        return {"success": False}
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client:
            async with client.post(pero_url.rstrip("/") + "/internal/v1/private/redeem",
                                   headers={"Authorization": "Bearer " + internal_token},
                                   json={"channel_id": channel_id, "account_id": account_id,
                                         "user_ref": user_ref, "user_wxid": user_wxid,
                                         "activation_code": activation_code,
                                         "source_event_id": event_id,
                                         "display_name": display_name}) as resp:
                if resp.status >= 400:
                    return {"success": False}
                return await resp.json()
    except Exception as exc:
        log.warning("Private activation request failed (%s)", type(exc).__name__)
        return {"success": False}


def is_activation_code_format(text: str) -> bool:
    # Full plain text only: no quote/XML parsing, embedded text or OCR.
    return isinstance(text, str) and re.fullmatch(r"PERO-[A-Za-z0-9_-]{16,128}", text.strip()) is not None


class PrivateAttempts:
    """开通码兑换的有界限流表：每用户冷却 + 总量上限（FIFO 淘汰最旧）。

    只在**低频发现流程**里使用：正常聊天轮询不碰兑换，也不因此写任何状态。
    """

    def __init__(self, cooldown: float = 5.0, limit: int = 2048,
                 clock=time.monotonic) -> None:
        self._cooldown = float(cooldown)
        self._limit = max(1, int(limit))
        self._clock = clock
        self._last: "collections.OrderedDict[str, float]" = collections.OrderedDict()

    def allow(self, ref: str) -> bool:
        """是否允许为这个 user_ref 再尝试一次兑换；允许时立即记账。"""
        key = str(ref or "")
        if not key:
            return False
        now = self._clock()
        previous = self._last.get(key)
        if previous is not None and now - previous < self._cooldown:
            return False
        if key in self._last:
            self._last.move_to_end(key)
        elif len(self._last) >= self._limit:
            self._last.popitem(last=False)
        self._last[key] = now
        return True

    def __len__(self) -> int:
        return len(self._last)
