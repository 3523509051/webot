"""图片缓存索引：每群独立编号、全量保留、单文件存储。

旧版缓存按 `<md5>.jpg` / `t<create_time>.jpg` 存两份，既浪费磁盘又无法让模型
按编号引用。这里改成：
  · 每张图只存一份 `cache/image/g<group_hash>_<seq>.jpg`；
  · SQLite 索引记录 group_wxid / seq / md5 / create_time / file / sender / context；
  · seq 每群独立，从 1 开始；
  · 不做任何自动裁剪（保留所有）。
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import time
from pathlib import Path


class ImageIndex:
    def __init__(self, db_path: str | Path, image_dir: str | Path) -> None:
        self.db_path = Path(db_path)
        self.image_dir = Path(image_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_wxid TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    md5 TEXT NOT NULL DEFAULT '',
                    md5s TEXT NOT NULL DEFAULT '',
                    create_time INTEGER NOT NULL DEFAULT 0,
                    file TEXT NOT NULL,
                    sender TEXT NOT NULL DEFAULT '',
                    context TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    UNIQUE(group_wxid, seq)
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_images_group_seq "
                "ON images(group_wxid, seq)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_images_group_md5 "
                "ON images(group_wxid, md5)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_images_group_time "
                "ON images(group_wxid, create_time)"
            )

    @staticmethod
    def _group_hash(group_wxid: str) -> str:
        return hashlib.sha1(group_wxid.encode("utf-8")).hexdigest()[:10]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    def add(
        self,
        group_wxid: str,
        source: str | Path,
        md5s: list[str] | None = None,
        create_time: int | None = None,
        sender: str = "",
        context: str = "",
    ) -> dict | None:
        """复制图片到缓存并分配本群编号；重复 md5 直接返回已有记录。"""
        src = Path(source)
        if not src.is_file() or src.stat().st_size <= 0:
            return None
        md5_list = [str(m).lower() for m in (md5s or []) if m]
        primary_md5 = md5_list[0] if md5_list else ""
        if primary_md5:
            existing = self.find_by_md5(primary_md5, group_wxid)
            if existing:
                return existing

        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM images WHERE group_wxid = ?",
                (group_wxid,),
            ).fetchone()
            seq = int(row["n"])
            name = f"g{self._group_hash(group_wxid)}_{seq}.jpg"
            dest = self.image_dir / name
            shutil.copyfile(src, dest)
            con.execute(
                "INSERT INTO images "
                "(group_wxid, seq, md5, md5s, create_time, file, sender, context, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    group_wxid,
                    seq,
                    primary_md5,
                    ",".join(md5_list),
                    int(create_time or 0),
                    str(dest),
                    sender or "",
                    str(context or "")[:200],
                    time.time(),
                ),
            )
            con.commit()
            return self.get(group_wxid, seq)
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def get(self, group_wxid: str, seq: int) -> dict | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM images WHERE group_wxid = ? AND seq = ?",
                (group_wxid, int(seq)),
            ).fetchone()
        finally:
            con.close()
        item = self._row_to_dict(row)
        if not item:
            return None
        if not Path(item["file"]).is_file():
            return None
        return item

    @staticmethod
    def _parse_since(value: str | int | None) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            n = int(text)
            return n // 1000 if n > 10_000_000_000 else n
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                import datetime as _dt

                return int(_dt.datetime.strptime(text, fmt).timestamp())
            except ValueError:
                continue
        return None

    def list(
        self,
        group_wxid: str,
        sender: str = "",
        since: str | int | None = None,
        limit: int = 20,
    ) -> dict:
        limit = max(1, min(int(limit or 20), 50))
        sql = "SELECT * FROM images WHERE group_wxid = ?"
        args: list = [group_wxid]
        if sender:
            sql += " AND sender LIKE ?"
            args.append(f"%{sender}%")
        ts = self._parse_since(since)
        if ts is not None:
            sql += " AND create_time >= ?"
            args.append(ts)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit + 1)
        con = self._connect()
        try:
            rows = con.execute(sql, args).fetchall()
        finally:
            con.close()
        items = [self._row_to_dict(r) for r in rows[:limit]]
        return {"items": items, "has_more": len(rows) > limit}

    def find_by_md5(self, md5: str, group_wxid: str | None = None) -> dict | None:
        md5 = str(md5 or "").lower()
        if not md5:
            return None
        sql = "SELECT * FROM images WHERE (md5 = ? OR md5s LIKE ?)"
        args: list = [md5, f"%{md5}%"]
        if group_wxid:
            sql += " AND group_wxid = ?"
            args.append(group_wxid)
        sql += " ORDER BY id DESC LIMIT 1"
        con = self._connect()
        try:
            row = con.execute(sql, args).fetchone()
        finally:
            con.close()
        item = self._row_to_dict(row)
        if item and not Path(item["file"]).is_file():
            return None
        return item

    def find_by_time(
        self, create_time: int, group_wxid: str | None = None, tolerance: int = 3
    ) -> dict | None:
        try:
            want = int(create_time)
        except (TypeError, ValueError):
            return None
        sql = "SELECT *, ABS(create_time - ?) AS d FROM images WHERE 1 = 1"
        args: list = [want]
        if group_wxid:
            sql += " AND group_wxid = ?"
            args.append(group_wxid)
        sql += " AND ABS(create_time - ?) <= ? ORDER BY d ASC, id DESC LIMIT 1"
        args.extend([want, max(0, int(tolerance))])
        con = self._connect()
        try:
            row = con.execute(sql, args).fetchone()
        finally:
            con.close()
        item = self._row_to_dict(row)
        if item and not Path(item["file"]).is_file():
            return None
        return item
