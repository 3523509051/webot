"""Reserve daily request allowance before I/O, shared across groups."""
from __future__ import annotations

import asyncio
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import time


class SearchQuota:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.day = lambda: time.strftime("%Y-%m-%d")

    def _read(self):
        today = self.day()
        if not self.path.exists():
            return {"date": today, "count": 0, "success": 0, "failed": 0, "empty": 0}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("搜索计数文件格式错误")
        date.fromisoformat(data.get("date", ""))
        if "count" not in data:
            raise ValueError("搜索计数文件缺少请求次数")
        if data.get("date") != today:
            return {"date": today, "count": 0, "success": 0, "failed": 0, "empty": 0}
        for key in ("count", "success", "failed", "empty"):
            value = data.get(key, 0)
            if type(value) is not int or value < 0:
                raise ValueError("搜索计数文件包含无效数值")
            data[key] = value
        return data

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".search-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def remaining(self, limit: int) -> int:
        return max(0, int(limit) - self._read()["count"])

    async def reserve(self, limit: int) -> str | None:
        async with self.lock:
            data = self._read()
            if data["count"] >= max(0, int(limit)):
                return None
            data["count"] += 1
            self._write(data)  # Persistence failure prevents the outgoing request.
            return data["date"]

    async def finish(self, day: str, outcome: str):
        async with self.lock:
            data = self._read()
            if data["date"] != day:
                return  # A request crossing midnight must not consume the new day's allowance.
            if outcome in ("success", "empty"):
                data["success"] += 1
                if outcome == "empty":
                    data["empty"] += 1
            else:
                data["failed"] += 1
            self._write(data)
