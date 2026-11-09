# -*- coding: utf-8 -*-
"""L2 静默流水：群里所有「聊天内容」的滚动记录，超长后摘要归档进 L4。

为什么不直接用 AstrBot 的 conversations：
  · 它只在**真正发生 LLM 请求**时才有内容 —— 没人 @ 机器人的时候一条都不写；
  · 它记录的是完整交互（含 tool 调用、工具报错/回执），对「聊天记忆」没有价值。
这里只记对话正文，与 L1 同源同格式（user/text/ts/at），只是长度长得多。

存储：`data/plugin_data/<plugin>/chatlog/<gid>.jsonl`，一行一条，重启可读回。
"""
from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path


class ChatLog:
    def __init__(self, cfg, data_dir, logger=None) -> None:
        self.cfg = cfg
        self.logger = logger
        self.dir = Path(data_dir) / "chatlog"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._mem: dict[str, deque] = {}

    # ── 内部 ──────────────────────────────────────────
    def _limit(self) -> int:
        return max(20, int(getattr(self.cfg, "l2_keep_items", 200) or 200))

    def _path(self, gid: str) -> Path:
        safe = "".join(c for c in str(gid) if c.isalnum() or c in "-_") or "unknown"
        return self.dir / f"{safe}.jsonl"

    def _load(self, gid: str) -> deque:
        dq = self._mem.get(gid)
        if dq is not None:
            return dq
        dq = deque(maxlen=self._limit())
        p = self._path(gid)
        if p.is_file():
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            dq.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                pass
        self._mem[gid] = dq
        return dq

    def _flush(self, gid: str) -> None:
        dq = self._load(gid)
        p = self._path(gid)
        tmp = p.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for it in dq:
                    f.write(json.dumps(it, ensure_ascii=False) + "\n")
            os.replace(tmp, p)
        except OSError as e:  # noqa: BLE001
            if self.logger:
                self.logger.debug("[group_activation] chatlog 落盘失败: %s", e)

    # ── 对外 ──────────────────────────────────────────
    def push(self, gid: str, item: dict) -> None:
        """记一条聊天（与 L1 同格式：user / text / ts / at）。"""
        dq = self._load(gid)
        dq.append(item)
        # 每 5 条落一次盘；第一条立即落盘（否则重启前一个文件都看不到，
        # 排查时无从下手）。丢的上限是最近 4 条，可以接受。
        if len(dq) == 1 or len(dq) % 5 == 0:
            self._flush(gid)

    def items(self, gid: str) -> list[dict]:
        return list(self._load(gid))

    def size(self, gid: str) -> int:
        return len(self._load(gid))

    def overflow(
        self,
        gid: str,
        keep: int,
        ratio: float = 0.25,
        batch: int = 0,
    ) -> list[dict]:
        """返回待归档的一批（超出 keep 的部分）。

        取多少：给了 batch 就用 batch；否则按 ratio 取当前总量的**比例**
        （默认 25%，最少 50 条）—— 这样流水越长、一次清的越多，
        不会在 500 条附近反复小幅归档。
        """
        dq = self._load(gid)
        extra = len(dq) - max(1, keep)
        if extra <= 0:
            return []
        if batch > 0:
            n = min(extra, max(1, batch))
        else:
            r = max(0.05, min(0.8, float(ratio or 0.25)))
            n = min(extra, max(50, int(len(dq) * r)))
        return list(dq)[:n]

    def drop(self, gid: str, n: int) -> None:
        dq = self._load(gid)
        for _ in range(min(n, len(dq))):
            dq.popleft()
        self._flush(gid)
