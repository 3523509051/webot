"""本地表情库：D:\\webot\\stickers\\index.json + files/。

设计原则：
  · 文件名给人看，index.json 给 AI/程序查；
  · AI 只新增，不覆盖/删除；
  · sha256 去重；同名自动加序号；
  · 只接受桥图片缓存里的真实文件（由 main.py 校验后传入）。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path


_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class StickerLibrary:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.files = self.root / "files"
        self.manifest = self.root / "index.json"
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.files.mkdir(parents=True, exist_ok=True)

    # ── manifest ─────────────────────────────────────
    def _load(self) -> dict:
        if not self.manifest.is_file():
            return {"version": 1, "next": 1, "items": {}}
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("manifest root is not object")
        except Exception:
            # 坏文件不覆盖用户数据；挪到旁边，重新开始
            backup = self.manifest.with_suffix(f".broken-{int(time.time())}.json")
            try:
                self.manifest.replace(backup)
            except OSError:
                pass
            return {"version": 1, "next": 1, "items": {}}
        data.setdefault("version", 1)
        data.setdefault("next", 1)
        data.setdefault("items", {})
        return data

    def _save(self, data: dict) -> None:
        tmp = self.manifest.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.manifest)

    @staticmethod
    def _clean(text: str, limit: int = 40) -> str:
        text = _BAD.sub("", str(text or "")).strip().strip(". ")
        return text[:limit]

    @staticmethod
    def _as_list(value) -> list[str]:
        if isinstance(value, (list, tuple)):
            raw = value
        else:
            raw = re.split(r"[,，、/|;；\s]+", str(value or ""))
        out: list[str] = []
        for item in raw:
            s = str(item).strip()
            if s and s not in out:
                out.append(s[:30])
        return out[:12]

    @staticmethod
    def _digest(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    # ── API ──────────────────────────────────────────
    def add(
        self,
        source: str | Path,
        name: str,
        emotion: str = "",
        scenes=None,
        tags=None,
        description: str = "",
        group: str = "",
        sender: str = "",
        image_id: int | None = None,
    ) -> dict:
        src = Path(source)
        if not src.is_file() or src.stat().st_size <= 0:
            raise ValueError("源图片不存在或为空")
        digest = self._digest(src)
        with self._lock:
            data = self._load()
            # 同一张图只存一份；重复时补齐用户给的标签
            for sid, item in data["items"].items():
                if item.get("sha256") == digest:
                    merged = self._as_list(item.get("tags")) + self._as_list(tags)
                    item["tags"] = list(dict.fromkeys(merged))[:12]
                    if emotion and not item.get("emotion"):
                        item["emotion"] = self._clean(emotion, 20)
                    self._save(data)
                    return {**item, "id": sid, "duplicate": True}

            seq = int(data.get("next") or 1)
            sid = f"st_{seq:04d}"
            data["next"] = seq + 1
            safe_name = self._clean(name) or "表情"
            safe_emotion = self._clean(emotion, 20) or "其他"
            suffix = src.suffix.lower()
            if suffix not in (".gif", ".png", ".jpg", ".jpeg", ".webp"):
                suffix = ".jpg"
            filename = f"{sid}_{safe_emotion}_{safe_name}{suffix}"
            dest = self.files / filename
            shutil.copyfile(src, dest)
            item = {
                "file": f"files/{filename}",
                "name": safe_name,
                "emotion": safe_emotion,
                "scenes": self._as_list(scenes),
                "tags": self._as_list(tags),
                "description": str(description or "")[:200],
                "source": {
                    "group": str(group or ""),
                    "sender": str(sender or ""),
                    "image_id": int(image_id) if image_id is not None else None,
                },
                "sha256": digest,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "added_by": "ai",
                "uses": 0,
            }
            data["items"][sid] = item
            self._save(data)
            return {**item, "id": sid, "duplicate": False}

    def find(self, sticker_id: str) -> dict | None:
        with self._lock:
            item = self._load()["items"].get(str(sticker_id or ""))
            if not item:
                return None
            path = (self.root / item["file"]).resolve()
            try:
                path.relative_to(self.files.resolve())
            except ValueError:
                return None
            if not path.is_file():
                return None
            return {**item, "id": str(sticker_id), "path": str(path)}

    def search(
        self,
        query: str = "",
        emotion: str = "",
        scene: str = "",
        limit: int = 12,
    ) -> list[dict]:
        q = str(query or "").strip().lower()
        emo = str(emotion or "").strip().lower()
        sc = str(scene or "").strip().lower()
        try:
            limit = max(1, min(int(limit or 12), 30))
        except (TypeError, ValueError):
            limit = 12
        with self._lock:
            items = list(self._load()["items"].items())
        out = []
        for sid, item in items:
            hay = " ".join([
                str(item.get("name") or ""),
                str(item.get("emotion") or ""),
                str(item.get("description") or ""),
                " ".join(item.get("scenes") or []),
                " ".join(item.get("tags") or []),
            ]).lower()
            if q and q not in hay:
                continue
            if emo and emo not in str(item.get("emotion") or "").lower():
                continue
            if sc and sc not in " ".join(item.get("scenes") or []).lower():
                continue
            out.append({
                "id": sid,
                "name": item.get("name") or "",
                "emotion": item.get("emotion") or "",
                "scenes": item.get("scenes") or [],
                "tags": item.get("tags") or [],
                "description": item.get("description") or "",
                "uses": int(item.get("uses") or 0),
            })
        out.sort(key=lambda x: (-x["uses"], x["id"]))
        return out[:limit]

    def mark_used(self, sticker_id: str) -> None:
        with self._lock:
            data = self._load()
            item = data["items"].get(str(sticker_id or ""))
            if item:
                item["uses"] = int(item.get("uses") or 0) + 1
                item["last_used_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._save(data)
