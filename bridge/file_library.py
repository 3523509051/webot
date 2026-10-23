"""本地文件库：D:\\webot\\files\\index.json + files/。

只接收文本/代码类文件；可执行文件和压缩包不下载、不入库。
AI 通过 list_files / read_file 按需访问，不把内容注入记忆。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path

# 允许的文本/代码后缀；不在表里的一律拒收（宁可少收，不执行/不打开二进制）
ALLOWED_EXT = {
    ".md", ".markdown", ".txt", ".text", ".rst",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx",
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt",
    ".go", ".rs", ".cs", ".php", ".rb", ".swift", ".lua",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".html", ".htm", ".css", ".scss", ".less", ".sql",
    ".csv", ".tsv", ".log", ".tex", ".cmake", ".mk",
}

ARCHIVE_EXT = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".zst", ".lz4", ".cab", ".iso", ".apk", ".jar", ".war",
}
EXEC_EXT = {
    ".exe", ".com", ".msi", ".dll", ".so", ".dylib", ".scr",
    ".bat", ".cmd", ".ps1", ".sh", ".vbs",
}

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class FileLibrary:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.files = self.root / "files"
        self.manifest = self.root / "index.json"
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.files.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def classify(name: str) -> tuple[str, str]:
        """返回 (accept/reject, 原因)。"""
        ext = Path(str(name or "")).suffix.lower()
        if not ext:
            return "reject", "没有扩展名"
        if ext in ARCHIVE_EXT:
            return "reject", "压缩包不接收"
        if ext in EXEC_EXT:
            return "reject", "可执行/脚本文件不接收"
        if ext not in ALLOWED_EXT:
            return "reject", f"不支持的文件类型 {ext}"
        return "accept", ""

    def _load(self) -> dict:
        if not self.manifest.is_file():
            return {"version": 1, "next": 1, "items": {}}
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("manifest root")
        except Exception:
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
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.manifest)

    @staticmethod
    def _clean(name: str, limit: int = 80) -> str:
        name = _BAD.sub("", str(name or "")).strip().strip(". ")
        return name[:limit] or "file"

    @staticmethod
    def _digest(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def add(
        self,
        source: str | Path,
        name: str,
        group: str = "",
        sender: str = "",
        source_local_id=None,
        max_bytes: int = 5_000_000,
    ) -> dict:
        src = Path(source)
        if not src.is_file() or src.stat().st_size <= 0:
            raise ValueError("文件不存在或为空")
        if src.stat().st_size > int(max_bytes):
            raise ValueError(f"文件超过 {max_bytes} 字节上限")
        verdict, reason = self.classify(name)
        if verdict != "accept":
            raise ValueError(reason)
        digest = self._digest(src)
        safe = self._clean(name)
        with self._lock:
            data = self._load()
            for fid, item in data["items"].items():
                if (
                    item.get("sha256") == digest
                    and item.get("group") == str(group or "")
                    and item.get("name") == safe
                ):
                    return {**item, "id": fid, "duplicate": True}
            seq = int(data.get("next") or 1)
            fid = f"f_{seq:04d}"
            data["next"] = seq + 1
            suffix = Path(safe).suffix.lower()
            dest = self.files / f"{fid}_{Path(safe).stem}{suffix}"
            # 重名也安全：fid 已唯一
            shutil.copyfile(src, dest)
            item = {
                "file": f"files/{dest.name}",
                "name": safe,
                "ext": suffix,
                "size": src.stat().st_size,
                "sha256": digest,
                "group": str(group or ""),
                "sender": str(sender or ""),
                "source_local_id": source_local_id,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "added_by": "bridge",
            }
            data["items"][fid] = item
            self._save(data)
            return {**item, "id": fid, "duplicate": False}

    def get(self, fid: str, group: str | None = None) -> dict | None:
        with self._lock:
            item = self._load()["items"].get(str(fid or ""))
            if not item:
                return None
            if group and item.get("group") != group:
                return None
            path = (self.root / item["file"]).resolve()
            try:
                path.relative_to(self.files.resolve())
            except ValueError:
                return None
            if not path.is_file():
                return None
            return {**item, "id": str(fid), "path": str(path)}

    def list(self, group: str | None = None, query: str = "", ext: str = "", limit: int = 20) -> list[dict]:
        q = str(query or "").strip().lower()
        ex = str(ext or "").strip().lower()
        if ex and not ex.startswith("."):
            ex = "." + ex
        try:
            limit = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError):
            limit = 20
        with self._lock:
            items = list(self._load()["items"].items())
        out = []
        for fid, item in items:
            if group and item.get("group") != group:
                continue
            if ex and item.get("ext") != ex:
                continue
            hay = f"{item.get('name','')} {item.get('sender','')} {item.get('ext','')}".lower()
            if q and q not in hay:
                continue
            out.append({
                "id": fid,
                "name": item.get("name") or "",
                "ext": item.get("ext") or "",
                "size": int(item.get("size") or 0),
                "sender": item.get("sender") or "",
                "created_at": item.get("created_at") or "",
            })
        out.sort(key=lambda x: x["id"], reverse=True)
        return out[:limit]
