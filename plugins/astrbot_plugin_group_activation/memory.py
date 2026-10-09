# -*- coding: utf-8 -*-
"""
L3 长期记忆：按群隔离的**结构化**「群员画像」。

为什么不用向量库（知识库）：
    「某群员的性格爱好」有三个特征，恰好和语义检索的适用场景相反 ——
      ① 结构化（人 → 属性），需要**按人精确查**而不是语义相似
      ② 会变化，需要**覆盖 / 遗忘**
      ③ 必须**按群隔离**（A 群的画像不能漏到 B 群）
    用 JSON 做 KV 存储 + 按群注入，比塞进向量库更简单、更可控。

存储结构（data/plugin_data/<plugin>/long_term_memory.json）：
    {
      "<群id>": {
        "updated_at": "2026-10-07T20:30:00",
        "members": {
          "小泽布尔": {
            "updated_at": "...",
            "facts": [
              {"text": "喜欢调 AI、测机器人", "first_ts": "...", "last_ts": "...", "hits": 3}
            ]
          }
        }
      }
    }

要点：
  · 每条事实都带**首次出现时间**与**最近提及时间**（用户要求把时间记下来），
    渲染时带上最近时间，让模型知道「这是什么时候的印象」。
  · 原子写入（临时文件 + os.replace），避免写一半把文件写坏。
  · 提炼由插件在**窗口休眠后异步**触发，不占用正常回复的延迟。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_NORM_RE = re.compile(r"[\s，。！？、,.!?；;：:]+")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def short_ts(iso: str) -> str:
    """2026-10-07T20:30:00 → 10-07"""
    m = re.match(r"\d{4}-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(1)}-{m.group(2)}" if m else ""


def _norm(text: str) -> str:
    return _NORM_RE.sub("", (text or "").strip()).lower()


@dataclass
class Fact:
    text: str
    first_ts: str = ""
    last_ts: str = ""
    hits: int = 1

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "hits": self.hits,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Fact":
        return cls(
            text=str(d.get("text", "")),
            first_ts=str(d.get("first_ts", "")),
            last_ts=str(d.get("last_ts", "")),
            hits=int(d.get("hits", 1) or 1),
        )


@dataclass
class Member:
    name: str
    facts: list[Fact] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "updated_at": self.updated_at,
            "facts": [f.to_dict() for f in self.facts],
        }

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "Member":
        return cls(
            name=name,
            facts=[Fact.from_dict(x) for x in (d.get("facts") or [])],
            updated_at=str(d.get("updated_at", "")),
        )


class LongTermMemory:
    """按群隔离的结构化长期记忆。"""

    def __init__(self, cfg, data_dir: Path, logger=None) -> None:
        self.cfg = cfg
        self.log = logger
        self.path = Path(data_dir) / "long_term_memory.json"
        self._data: dict[str, Any] = {}
        self.load()

    # ── 持久化 ────────────────────────────────────────
    def load(self) -> None:
        try:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f) or {}
        except Exception as e:  # noqa: BLE001
            self._warn(f"读取长期记忆失败（将视为空）: {e}")
            self._data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".ltm-", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # 原子替换
        except Exception as e:  # noqa: BLE001
            self._warn(f"写入长期记忆失败: {e}")

    # ── 读 ────────────────────────────────────────────
    def members(self, gid: str) -> dict[str, Member]:
        raw = (self._data.get(gid) or {}).get("members") or {}
        return {name: Member.from_dict(name, d) for name, d in raw.items()}

    def total_facts(self, gid: str) -> int:
        return sum(len(m.facts) for m in self.members(gid).values())

    # ── 写 ────────────────────────────────────────────
    def add_facts(self, gid: str, member: str, facts: list[str]) -> int:
        """合并式写入。文本归一化后相同则只更新 last_ts 并累加 hits。

        Returns:
            新增的事实条数。
        """
        member = (member or "").strip()
        texts = [t.strip() for t in (facts or []) if (t or "").strip()]
        if not member or not texts:
            return 0

        ts = now_iso()
        bucket = self._data.setdefault(gid, {"members": {}})
        members = bucket.setdefault("members", {})
        m = members.setdefault(member, {"facts": [], "updated_at": ts})

        index = {_norm(f.get("text", "")): f for f in m["facts"]}
        added = 0
        for t in texts:
            key = _norm(t)
            if not key:
                continue
            if key in index:
                index[key]["last_ts"] = ts
                index[key]["hits"] = int(index[key].get("hits", 1)) + 1
            else:
                item = {"text": t, "first_ts": ts, "last_ts": ts, "hits": 1}
                m["facts"].append(item)
                index[key] = item
                added += 1

        # 容量控制：超出上限时丢最久未被提及的
        cap = int(getattr(self.cfg, "l3_facts_cap_per_member", 12) or 12)
        if len(m["facts"]) > cap:
            m["facts"].sort(key=lambda f: f.get("last_ts", ""), reverse=True)
            del m["facts"][cap:]

        m["updated_at"] = ts
        bucket["updated_at"] = ts
        self.save()
        return added

    def forget(self, gid: str | None = None) -> None:
        """清空某个群（或全部）的长期记忆。"""
        if gid is None:
            self._data = {}
        else:
            self._data.pop(gid, None)
        self.save()

    # ── 渲染（注入用）────────────────────────────────
    def render(self, gid: str) -> str:
        members = self.members(gid)
        if not members:
            return ""

        max_members = int(getattr(self.cfg, "l3_max_members", 8) or 8)
        max_facts = int(getattr(self.cfg, "l3_max_facts_per_member", 4) or 4)

        ordered = sorted(
            members.values(),
            key=lambda m: max((f.last_ts for f in m.facts), default=""),
            reverse=True,
        )[:max_members]

        lines: list[str] = []
        for m in ordered:
            facts = sorted(m.facts, key=lambda f: f.last_ts, reverse=True)[:max_facts]
            if not facts:
                continue
            parts = []
            for f in facts:
                stamp = short_ts(f.last_ts)
                parts.append(f"{f.text}（{stamp}）" if stamp else f.text)
            lines.append(f"- {m.name}：" + "；".join(parts))

        if not lines:
            return ""
        header = (getattr(self.cfg, "l3_header", "") or "").strip()
        return (header + "\n" if header else "") + "\n".join(lines)

    # ── 调试 ──────────────────────────────────────────
    def dump(self, gid: str, limit: int = 12) -> str:
        members = self.members(gid)
        if not members:
            return "本群暂无长期记忆。"
        out = []
        for name, m in sorted(
            members.items(),
            key=lambda kv: kv[1].updated_at,
            reverse=True,
        )[:limit]:
            for f in sorted(m.facts, key=lambda x: x.last_ts, reverse=True):
                out.append(
                    f"· {name}: {f.text}"
                    f"  [首见 {short_ts(f.first_ts)} / 最近 {short_ts(f.last_ts)} / {f.hits}次]"
                )
        return "\n".join(out) if out else "本群暂无长期记忆。"

    def _warn(self, msg: str) -> None:
        if self.log:
            self.log.warning("[group_activation] %s", msg)
