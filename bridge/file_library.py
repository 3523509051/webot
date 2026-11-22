"""本地文件库：D:\\webot\\files\\index.json + files/。

接收文本/代码、文档与演示、电子书，以及作为文件附件收到的图片；
无扩展名/未知后缀先下载、再用有界内容嗅探决定是否接收（像文本才收）。
压缩包与可执行二进制作为**附件原件**保存登记，但绝不执行、启动或自动解压；
文本脚本只作为数据读取，绝不执行。
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

# 已知二进制文档/图片：按容器接收（PDF/Office/电子书/图片不做文本嗅探）。
BINARY_EXT = {
    # 文档与演示（Pero 侧 PDF/Office 用 recognition，电子书 user_data）
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".epub", ".mobi",
    # 文件附件形式的图片：普通格式仍按图片流程，BMP/TIFF 走文档解析
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
}

# 文本/源码后缀：接收前需通过有界内容嗅探（防二进制改名 .txt 放行）。
TEXT_EXT = {
    ".md", ".markdown", ".txt", ".text", ".rst",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx",
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt",
    ".go", ".rs", ".cs", ".php", ".rb", ".swift", ".lua",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".html", ".htm", ".css", ".scss", ".less", ".sql",
    ".csv", ".tsv", ".log", ".tex", ".cmake", ".mk",
    # 文本脚本只作为数据读取，绝不执行（可执行二进制仍在 EXEC_EXT 拒绝）。
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd", ".vbs",
}

ALLOWED_EXT = BINARY_EXT | TEXT_EXT

ARCHIVE_EXT = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".zst", ".lz4", ".cab", ".iso", ".apk", ".jar", ".war",
}
EXEC_EXT = {
    ".exe", ".com", ".msi", ".dll", ".so", ".dylib", ".scr",
}

#: 文本嗅探读取上限（有界；不为识别类型读全文）。
TEXT_SNIFF_BYTES = 8192

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def looks_like_textual(path: str | Path, limit: int = TEXT_SNIFF_BYTES) -> bool:
    """有界判定文件头是否像文本：UTF-8 严格优先，其次 GB18030 且控制字符极少。

    含 NUL 直接判否（二进制容器/可执行文件）；允许读取上限截断多字节字符
    造成的尾部解码错误，不把正常中文误判。
    """

    try:
        with Path(path).open("rb") as stream:
            head = stream.read(int(limit))
    except OSError:
        return False
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        if exc.start >= len(head) - 3:
            try:
                head[: exc.start].decode("utf-8")
                return True
            except UnicodeDecodeError:
                pass
    try:
        text = head.decode("gb18030")
    except UnicodeDecodeError:
        return False
    control = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\r\n")
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\t\r\n")
    return printable >= max(1, int(len(text) * 0.95)) and control <= 1


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
        """返回 (accept/verify/sniff/reject, 原因)。

        - accept：已知二进制文档/图片，扩展名即可确定；
        - verify：已知文本/源码后缀，下载后必须有界校验内容（防改名放行）；
        - sniff：无扩展名或未知后缀，下载后内容像文本才接收；
        - reject：压缩包、可执行二进制（`.sh/.ps1` 等文本脚本按 verify 处理）。
        """

        raw = str(name or "")
        ext = Path(raw).suffix.lower()
        if ext in ARCHIVE_EXT or ext in EXEC_EXT:
            # 二进制附件（压缩包/可执行）：保存原件用于登记与追溯，
            # 绝不执行、启动或自动解压；供应商不支持解析时如实报告。
            return "accept", ""
        if ext in BINARY_EXT:
            return "accept", ""
        if ext in TEXT_EXT:
            return "verify", ""
        return "sniff", ""

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
        # 默认 100 MiB（1 MiB=1,048,576）；可按调用方配置覆盖，不再写死 5 MB。
        max_bytes: int = 104_857_600,
    ) -> dict:
        src = Path(source)
        if not src.is_file() or src.stat().st_size <= 0:
            raise ValueError("文件不存在或为空")
        if src.stat().st_size > int(max_bytes):
            raise ValueError(f"文件超过 {max_bytes} 字节上限")
        verdict, reason = self.classify(name)
        if verdict == "reject":
            raise ValueError(reason)
        if verdict != "accept" and not looks_like_textual(src):
            raise ValueError("内容不像文本，已拒收（二进制不能靠改扩展名入库）")
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
