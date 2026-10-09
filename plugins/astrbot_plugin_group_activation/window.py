# -*- coding: utf-8 -*-
"""
唤醒窗口状态机（每群一份）。

    SLEEP ──@机器人──▶ ACTIVE(expire_at)
      ▲                     │
      └──空闲超时 / 存活上限─┘

滑动语义：窗口内每收到一条消息就把 expire_at 往后推（取 max）。
两条终止线：空闲超时（idle_seconds）、单次激活存活上限（window_max_seconds）。

设计要点（v0.3.0）：
  · 窗口**只决定"默认 LLM 是否处理本群消息"**，不产生任何回复内容。
  · 记忆与窗口**解耦**：L1 永远在记；窗口只控制"是否注入 L1"与"是否放行 LLM"。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class GroupState:
    group_id: str
    status: str = "SLEEP"          # SLEEP / ACTIVE
    expire_at: float = 0.0         # 滑动窗口到期时间
    activated_at: float = 0.0      # 本次激活起点
    l1: deque = field(default_factory=lambda: deque(maxlen=25))  # 群聊滚动背景


class WindowManager:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.states: dict[str, GroupState] = {}

    # ── 查询 ──────────────────────────────────────────
    def state(self, gid: str) -> GroupState:
        st = self.states.get(gid)
        if st is None:
            st = GroupState(group_id=gid, l1=deque(maxlen=self.cfg.l1_size))
            self.states[gid] = st
        return st

    def is_active(self, gid: str, now: float | None = None) -> bool:
        st = self.state(gid)
        now = now if now is not None else time.time()
        if st.status != "ACTIVE":
            return False
        if now >= st.expire_at or now - st.activated_at > self.cfg.window_max_seconds:
            self.sleep(gid)
            return False
        return True

    def active_count(self) -> int:
        now = time.time()
        return sum(1 for g in list(self.states) if self.is_active(g, now))

    def remaining(self, gid: str, now: float | None = None) -> float:
        st = self.state(gid)
        now = now if now is not None else time.time()
        return max(0.0, st.expire_at - now)

    # ── 变更 ──────────────────────────────────────────
    def activate(self, gid: str, now: float | None = None) -> GroupState:
        """@ 开关打开：重置窗口。"""
        now = now if now is not None else time.time()
        st = self.state(gid)
        st.status = "ACTIVE"
        st.activated_at = now
        st.expire_at = now + self.cfg.w0_seconds
        return st

    def touch(self, gid: str, now: float | None = None) -> None:
        """窗口内新消息：滑动续期。"""
        now = now if now is not None else time.time()
        st = self.state(gid)
        st.expire_at = max(st.expire_at, now + self.cfg.idle_seconds)

    def sleep(self, gid: str) -> None:
        st = self.state(gid)
        st.status = "SLEEP"
        st.expire_at = 0.0

    # ── 记忆（L1）────────────────────────────────────
    def push_l1(self, gid: str, item: dict) -> None:
        self.state(gid).l1.append(item)

    def render_l1(self, gid: str, exclude_last: bool = True) -> str:
        """把 L1 渲染成注入用的文本块。

        exclude_last=True 时去掉最后一条 —— 它就是当前这条消息，
        已经作为用户消息存在于请求里，重复一次既浪费 token 也容易让模型重复。
        """
        st = self.state(gid)
        items = list(st.l1)
        if exclude_last and items:
            items = items[:-1]
        if not items:
            return ""
        # 用「昵称」：内容显式标注，归属一眼可辨 —— 之前写成 `user: text`，
        # 多个人连着说话时模型会把不同人的话混在一起（「认错人」）。
        lines = [
            f"「{it['user']}」：{it['text']}" + ("   ← 这条在 @ 你" if it.get("at") else "")
            for it in items
        ]
        header = (self.cfg.l1_header or "").strip()
        return (header + "\n" if header else "") + "\n".join(lines)

    def snapshot(self) -> list[dict]:
        now = time.time()
        return [
            {
                "group_id": gid,
                "remaining": round(max(0.0, st.expire_at - now), 1),
                "l1": len(st.l1),
            }
            for gid, st in self.states.items()
            if st.status == "ACTIVE" and now < st.expire_at
        ]
