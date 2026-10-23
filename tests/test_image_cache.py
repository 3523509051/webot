# -*- coding: utf-8 -*-
"""图片按 md5 缓存的行为：收到图存一份，以后引用旧图直接命中。

2026-10-16 需求：表情包有 md5 缓存、随时能引用；图片却只能回微信本地库现解密
（`image_local_id_by_ref` 只扫最近 300 行），微信清理后引用就失败 → 给图片加同样的缓存。
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BRIDGE))
_spec = importlib.util.spec_from_file_location(
    "webot_wechat_client", BRIDGE / "wechat_client.py"
)
wc = importlib.util.module_from_spec(_spec)
sys.modules["webot_wechat_client"] = wc
_spec.loader.exec_module(wc)
sys.path.pop(0)


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = wc._IMAGE_CACHE_DIR
        wc._IMAGE_CACHE_DIR = Path(self.tmp.name) / "image"
        self.src = Path(self.tmp.name) / "pic.dat"
        self.src.write_bytes(b"picture-bytes" * 4)
        self.md5 = "0123456789abcdef0123456789abcdef"

    def tearDown(self):
        wc._IMAGE_CACHE_DIR = self._orig
        self.tmp.cleanup()

    def test_md5_picker_finds_all_hex_hashes(self):
        blob = (b"\x00\x01bffee8b9477c936098967b7f048b53a6\x00"
                b"0123456789abcdef0123456789abcdef")
        self.assertEqual(
            wc.md5s_in_packed_info(blob),
            ["bffee8b9477c936098967b7f048b53a6", "0123456789abcdef0123456789abcdef"],
        )
        self.assertEqual(wc.md5s_in_packed_info(b"\x00\x01"), [])
        self.assertEqual(wc.md5s_in_packed_info(""), [])

    def test_save_then_lookup(self):
        self.assertIsNone(wc.cached_image(self.md5))
        # 同时写入 md5 键与时间键
        self.assertEqual(wc.save_image_cache(str(self.src), [self.md5], 1792116838), 2)
        got = wc.cached_image(self.md5)
        self.assertIsNotNone(got)
        self.assertEqual(got.read_bytes(), self.src.read_bytes())
        self.assertEqual(wc.save_image_cache(str(self.src), [self.md5], 1792116838), 0)

    def test_time_key_lookup_with_tolerance(self):
        """引用 XML 的 createtime 是主要检索键，允许 ±3 秒容差。"""
        wc.save_image_cache(str(self.src), [], create_time=1792116838)
        self.assertIsNotNone(wc.cached_image_by_time(1792116838))
        self.assertIsNotNone(wc.cached_image_by_time("1792116840"))
        self.assertIsNotNone(wc.cached_image_by_time(1792116835))
        self.assertIsNone(wc.cached_image_by_time(1792116800))
        self.assertIsNone(wc.cached_image_by_time(None))

    def test_time_only_save_works_without_md5(self):
        """本地 packed_info 取不到 md5 时，只要有时间就能缓存（图片行偶尔如此）。"""
        self.assertEqual(wc.save_image_cache(str(self.src), [], 1792116838), 1)

    def test_disabled_and_prune(self):
        self.assertEqual(wc.save_image_cache(str(self.src), [self.md5], keep=0), 0)
        self.assertIsNone(wc.cached_image(self.md5))
        for i in range(5):
            wc.save_image_cache(str(self.src), [f"{i:032x}"], keep=3)
        files = list((Path(self.tmp.name) / "image").glob("*.jpg"))
        self.assertLessEqual(len(files), 3)

    def test_missing_source_is_ignored(self):
        self.assertEqual(
            wc.save_image_cache(str(Path(self.tmp.name) / "nope.jpg"), [self.md5]), 0
        )


if __name__ == "__main__":
    unittest.main()
