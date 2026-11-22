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
import uuid
from typing import Any, Awaitable, Callable

import websockets

log = logging.getLogger("onebot")

ActionHandler = Callable[[str, dict], Awaitable[Any]]


class ActionFailed(Exception):
    def __init__(self, message: str, *, delivery_state: str = "failed", retcode: int = 1200):
        super().__init__(message)
        self.delivery_state = delivery_state
        self.retcode = retcode


class BridgeSendError(RuntimeError):
    """入站上报没有真正发出去（可识别、可重试）。

    只用于上报调用；出站动作回执仍走 `_send_json(required=False)` 的尽力而为，
    不因这个异常改变既有语义。

    `code`：
      - `not_connected`：反向 WS 未连接（等待重连后重试）；
      - `send_failed`：写帧异常（连接可能已断，同样按可重试处理）。
    """

    retryable = True

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code
        self.detail = detail


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
        identity_directory=None,
    ) -> None:
        self.ws_url = ws_url
        self.self_id = self_id
        self.token = token
        self.heartbeat = heartbeat
        self.action_handler = action_handler
        self.identity_directory = identity_directory
        self.bridge_session = uuid.uuid4().hex

        self._ws = None
        self._msg_id = int(time.time() * 1_000_000)

    # ── 工具 ───────────────────────────────────────────
    def next_msg_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _send_json(self, obj: dict, *, required: bool = False) -> bool:
        """发一帧 JSON。

        `required=False`（默认）：心跳/动作回执等**尽力而为**的帧，未连接或失败
        只记日志、返回 False，不改变既有出站回执语义。

        `required=True`：入站上报必须**如实返回**结果——未连接抛 `not_connected`，
        发送异常抛 `send_failed`。调度层据此保留待投递事件重试，绝不把"没发出去"
        记成"已分发"。
        """
        ws = self._ws
        if ws is None:
            if required:
                raise BridgeSendError("not_connected")
            return False
        try:
            await ws.send(json.dumps(obj, ensure_ascii=False))
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("上报失败: %s", e)
            if required:
                # 只暴露异常类型，不带原始错误体/正文。
                raise BridgeSendError("send_failed", type(e).__name__) from e
            return False

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
                    reads = set()
                    try:
                        async for raw in ws:
                            try:
                                action = json.loads(raw).get("action")
                            except (ValueError, AttributeError):
                                action = None
                            if action in ("read_website", "browser_search"):
                                # Bounded read-only actions must not hold up group replies.
                                task = asyncio.create_task(self._on_raw(raw))
                                reads.add(task)
                                task.add_done_callback(reads.discard)
                            else:
                                await self._on_raw(raw)
                    finally:
                        hb.cancel()
                        for task in reads:
                            task.cancel()
                        await asyncio.gather(*reads, return_exceptions=True)
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
                if action.startswith("send_") and (
                    not isinstance(data, dict) or not data.get("message_id")
                ):
                    raise ActionFailed("发送动作没有返回有效消息ID")
            else:
                raise ActionFailed("动作处理器未配置")
        except ActionFailed as e:
            status, retcode = "failed", e.retcode
            data = {"error": str(e), "delivery_state": e.delivery_state,
                    "retry_safe": False}
        except Exception as e:  # noqa: BLE001
            log.exception("action 处理失败: %s", action)
            status, retcode = "failed", 1
            data = {"error": str(e), "retry_safe": False}

        await self._send_json(
            {"status": status, "retcode": retcode, "data": data, "echo": echo,
             "message": (data or {}).get("error", "") if isinstance(data, dict) else ""}
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
        identity: dict | None = None,
        pero_ext: dict | None = None,
        message_id: int | None = None,
    ) -> int:
        """segments 为标准 OneBot v11 消息段列表，例如
        [{"type": "at", "data": {"qq": "10001"}},
         {"type": "text", "data": {"text": "你好"}}]

        `pero_ext`（P13，可选）：版本化身份/引用/文件扩展，作为事件顶层
        `pero_ext` 字段下发。所有旧标准字段原样保留，旧 AstrBot 适配器会忽略
        未知顶层键；没有扩展时事件与升级前完全一致。

        `message_id`（可选）：**重试时必须复用同一个值**，对外事件 ID 才稳定，
        Pero 侧入站幂等（同一 source/event ID 只处理一次）才成立。
        返回值即本次事件的 `message_id`；未连上或写帧失败抛 `BridgeSendError`。
        """
        message = segments
        # raw_message 按 OneBot v11 规范应为「CQ 码字符串」。
        # 之前我们填的是明文（含 "@昵称"），会被适配器重复解析出 at 段，
        # 因此这里统一由 segments 生成规范的 CQ 码。
        raw_message = raw or _to_cq(segments)

        mid = int(message_id) if message_id else self.next_msg_id()
        if self.identity_directory is not None and identity:
            self.identity_directory.register_message(self.bridge_session, mid,
                str(identity['group_wxid']), identity)

        event = {
            "time": int(time.time()),
            "self_id": self.self_id,
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": mid,
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
        if isinstance(pero_ext, dict) and pero_ext:
            # 版本化扩展（P13）：只加顶层键，不改任何标准字段；
            # session（桥进程会话）在这里填，用于限定 local_id 的来源范围。
            pero_ext.setdefault("session", self.bridge_session)
            event["pero_ext"] = pero_ext
        await self._send_json(event, required=True)
        return mid

    async def send_private_message(self, user_id: int, segments: list[dict],
                                   nickname: str = "", pero_ext: dict | None = None,
                                   message_id: int | None = None) -> int:
        """同 `send_group_message`：复用 `message_id` 保证重试时事件身份稳定。"""
        ext = dict(pero_ext or {})
        ext["session"] = self.bridge_session
        mid = int(message_id) if message_id else self.next_msg_id()
        await self._send_json({"time": int(time.time()), "self_id": self.self_id,
                               "post_type": "message", "message_type": "private",
                               "sub_type": "friend", "message_id": mid,
                               "user_id": user_id, "message": segments,
                               "raw_message": _to_cq(segments), "font": 0,
                               "sender": {"user_id": user_id, "nickname": nickname},
                               "pero_ext": ext}, required=True)
        return mid
