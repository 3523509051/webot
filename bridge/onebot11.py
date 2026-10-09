# -*- coding: utf-8 -*-
"""
极简 OneBot v11 实现端（反向 WebSocket 客户端）。

本程序主动连接 AstrBot 的 aiocqhttp 服务（默认 ws://127.0.0.1:6199/ws），
负责：
  1. 上报 meta_event（lifecycle / heartbeat）
  2. 上报 message 事件（群 / 私聊）
  3. 响应 AstrBot 下发的 action（send_group_msg 等）
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

import websockets

log = logging.getLogger("onebot")

ActionHandler = Callable[[str, dict], Awaitable[Any]]


def _to_cq(segments: list[dict]) -> str:
    """把消息段转成 OneBot v11 的 CQ 码字符串。"""
    parts: list[str] = []
    for seg in segments:
        stype = seg.get("type")
        data = seg.get("data") or {}
        if stype == "text":
            parts.append(str(data.get("text", "")))
        elif stype == "at":
            parts.append(f"[CQ:at,qq={data.get('qq', '')}]")
        else:
            kv = ",".join(f"{k}={v}" for k, v in data.items())
            parts.append(f"[CQ:{stype},{kv}]" if kv else f"[CQ:{stype}]")
    return "".join(parts)


class OneBotClient:
    def __init__(
        self,
        ws_url: str,
        self_id: int,
        token: str = "",
        heartbeat: float = 15.0,
        action_handler: ActionHandler | None = None,
    ) -> None:
        self.ws_url = ws_url
        self.self_id = self_id
        self.token = token
        self.heartbeat = heartbeat
        self.action_handler = action_handler

        self._ws = None
        self._msg_id = 0

    # ── 工具 ───────────────────────────────────────────
    def next_msg_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _send_json(self, obj: dict) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(obj, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            log.warning("上报失败: %s", e)

    # ── 主循环 ─────────────────────────────────────────
    async def run(self) -> None:
        while True:
            try:
                # aiocqhttp 强制要求这两个握手头，否则握手被拒(400)
                #   X-Self-ID     : 机器人 QQ 号
                #   X-Client-Role : event / api / Universal
                headers = {
                    "X-Self-ID": str(self.self_id),
                    "X-Client-Role": "Universal",
                }
                if self.token:
                    headers["Authorization"] = f"Bearer {self.token}"
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    max_size=16 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    self._ws = ws
                    log.info("已连接 AstrBot -> %s", self.ws_url)
                    await self._send_lifecycle()
                    hb = asyncio.create_task(self._heartbeat_loop())
                    try:
                        async for raw in ws:
                            await self._on_raw(raw)
                    finally:
                        hb.cancel()
            except Exception as e:  # noqa: BLE001
                log.warning("连接断开/失败: %s；5 秒后重连", e)
            finally:
                self._ws = None
            await asyncio.sleep(5)

    async def _on_raw(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            log.warning("无法解析: %r", raw[:200])
            return

        action = payload.get("action")
        if action is None:
            return

        echo = payload.get("echo")
        status, retcode, data = "ok", 0, None
        try:
            if self.action_handler is not None:
                data = await self.action_handler(action, payload.get("params") or {})
        except Exception as e:  # noqa: BLE001
            log.exception("action 处理失败: %s", action)
            status, retcode = "failed", 1
            data = {"error": str(e)}

        await self._send_json(
            {"status": status, "retcode": retcode, "data": data, "echo": echo}
        )

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat)
            await self._send_json(
                {
                    "time": int(time.time()),
                    "self_id": self.self_id,
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                    "status": {"online": True, "good": True},
                    "interval": int(self.heartbeat * 1000),
                }
            )

    async def _send_lifecycle(self) -> None:
        await self._send_json(
            {
                "time": int(time.time()),
                "self_id": self.self_id,
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
            }
        )

    # ── 上报消息 ───────────────────────────────────────
    async def send_group_message(
        self,
        group_id: int,
        segments: list[dict],
        user_id: int = 0,
        nickname: str = "",
        raw: str = "",
    ) -> None:
        """segments 为标准 OneBot v11 消息段列表，例如
        [{"type": "at", "data": {"qq": "10001"}},
         {"type": "text", "data": {"text": "你好"}}]
        """
        message = segments
        # raw_message 按 OneBot v11 规范应为「CQ 码字符串」。
        # 之前我们填的是明文（含 "@昵称"），会被适配器重复解析出 at 段，
        # 因此这里统一由 segments 生成规范的 CQ 码。
        raw_message = raw or _to_cq(segments)

        await self._send_json(
            {
                "time": int(time.time()),
                "self_id": self.self_id,
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": self.next_msg_id(),
                "group_id": group_id,
                "user_id": user_id or self.self_id,
                "message": message,
                "raw_message": raw_message,
                "font": 0,
                "sender": {
                    "user_id": user_id or self.self_id,
                    "nickname": nickname or "unknown",
                    "card": "",
                    "role": "member",
                },
            }
        )
