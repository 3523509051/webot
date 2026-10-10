# -*- coding: utf-8 -*-
"""无需微信和付费模型，验证节流的关键路径。"""

from __future__ import annotations

import base64
import importlib.util
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ROUTING_PATH = ROOT / "plugins/astrbot_plugin_group_activation/routing.py"
spec = importlib.util.spec_from_file_location("webot_routing", ROUTING_PATH)
routing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routing)


class RoutingTests(unittest.TestCase):
    def test_ambient_and_same_speaker_chatter_are_free(self):
        self.assertFalse(routing.should_consider_followup("这个怎么弄？", linked=False))
        self.assertFalse(routing.should_consider_followup("哈哈哈", linked=True))
        self.assertFalse(routing.should_consider_followup("大家知道附近哪里有饭吃？", linked=True))

    def test_recent_real_followup_is_eligible(self):
        self.assertTrue(routing.should_consider_followup("这张图写了什么？", linked=True))
        self.assertTrue(routing.should_consider_followup("那你再解释一下", linked=True))
        self.assertFalse(routing.wants_image("今天吃什么"))
        self.assertTrue(routing.wants_image("这张图上面写着什么"))

    def test_prior_images_removed_without_changing_original(self):
        history = [
            {"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + "x" * 50000}},
            ]},
            {"role": "assistant", "content": "图里是一只猫"},
            {"role": "user", "content": "另外一个问题"},
        ]
        result = routing.compact_contexts(history, max_turns=2, max_chars=12000)
        self.assertEqual(result[0]["content"][1]["type"], "text")
        self.assertEqual(history[0]["content"][1]["type"], "image_url")
        self.assertEqual(result[1]["content"], "图里是一只猫")

    def test_history_keeps_full_recent_turns(self):
        history = []
        for n in range(5):
            history += [{"role": "user", "content": str(n)},
                        {"role": "assistant", "content": f"reply {n}"}]
        recent = routing.compact_contexts(history, max_turns=2, max_chars=1000)
        self.assertEqual([m["content"] for m in recent], ["3", "reply 3", "4", "reply 4"])


class ImageBridgeTests(unittest.TestCase):
    def test_large_photo_is_resized_before_upload(self):
        sys.path.insert(0, str(ROOT / "bridge"))
        try:
            import main as bridge
            bridge.MEDIA_OPTS["image_max_edge"] = 1280
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "photo.png"
                Image.new("RGB", (2400, 1400), "#ad83d0").save(path)
                segments = bridge.image_segment(str(path), 3_000_000)
                self.assertEqual(segments[0]["type"], "image")
                encoded = segments[0]["data"]["file"].removeprefix("base64://")
                with Image.open(BytesIO(base64.b64decode(encoded))) as resized:
                    self.assertEqual(max(resized.size), 1280)
        finally:
            sys.path.remove(str(ROOT / "bridge"))


if __name__ == "__main__":
    unittest.main()
