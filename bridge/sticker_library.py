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

# 群友/模型常用的近义情绪；只用于检索，不改写库里的标签
_EMOTION_SYNONYMS = {
    "开心": ("愉悦", "高兴", "快乐", "笑", "治愈", "可爱"),
    "高兴": ("开心", "愉悦", "快乐", "笑"),
    "快乐": ("开心", "愉悦", "高兴", "笑"),
    "生气": ("气鼓鼓", "愤怒", "凶", "不爽"),
    "愤怒": ("生气", "气鼓鼓", "凶"),
    "无语": ("嫌弃", "无奈", "冷漠", "呆滞", "超然"),
    "无奈": ("无语", "嫌弃", "冷漠"),
    "震惊": ("惊讶", "呆住", "超然", "呆滞"),
    "搞笑": ("沙雕", "魔性", "搞笑", "笑"),
    "喜欢": ("爱心", "贴贴", "可爱", "治愈"),
    "庆祝": ("开心", "愉悦", "高兴", "获奖"),
}


def _search_terms(value: str) -> set[str]:
    value = str(value or "").strip().lower()
    if not value:
        return set()
    terms = {value}
    for key, vals in _EMOTION_SYNONYMS.items():
        if key in value or value in key:
            terms.add(key)
            terms.update(vals)
    return terms


class StickerLibrary:
    def __init__(self, root: str | Path, default_root: str | Path | None = None) -> None:
        self.root = Path(root)
        self.files = self.root / "files"
        self.manifest = self.root / "index.json"
        self.default_root = (
            Path(default_root)
            if default_root is not None
            else Path(__file__).resolve().parent.parent / "stickers" / "default"
        )
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.files.mkdir(parents=True, exist_ok=True)
        self._seed_defaults()

    def _seed_defaults(self) -> None:
        """新设备运行时库为空时，把仓库里的默认表情复制进来。"""
        try:
            if self.manifest.is_file():
                data = json.loads(self.manifest.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("items"):
                    return
            src_manifest = self.default_root / "index.json"
            src_files = self.default_root / "files"
            if not src_manifest.is_file() or not src_files.is_dir():
                return
            shutil.copy2(src_manifest, self.manifest)
            shutil.copytree(src_files, self.files, dirs_exist_ok=True)
        except Exception:
            # 默认库只是锦上添花；失败不能阻止桥启动。
            pass

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
        q_terms = _search_terms(q)
        emo_terms = _search_terms(emo)
        sc_terms = _search_terms(sc)
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
            if q_terms and not any(t in hay for t in q_terms):
                continue
            if emo_terms and not any(t in hay for t in emo_terms):
                continue
            if sc_terms and not any(t in hay for t in sc_terms):
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
