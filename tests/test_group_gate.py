# -*- coding: utf-8 -*-
"""模拟 AstrBot 群消息入口，验证不必要的消息不会到达付费请求。"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def plugin_with_fake_astrbot(tmp: str):
    passthrough = lambda *a, **k: lambda fn: fn
    modules = {name: types.ModuleType(name) for name in (
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.provider",
        "astrbot.api.star", "astrbot.core", "astrbot.core.star",
        "astrbot.core.star.filter", "astrbot.core.star.filter.event_message_type",
    )}
    modules["astrbot.api"].logger = logging.getLogger("fake-astrbot")
    modules["astrbot.api"].star = types.SimpleNamespace(Star=object)
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        event_message_type=passthrough, on_llm_request=passthrough,
        on_decorating_result=passthrough, after_message_sent=passthrough,
        llm_tool=passthrough, command=passthrough,
    )
    modules["astrbot.api.provider"].ProviderRequest = object
    modules["astrbot.api.star"].StarTools = types.SimpleNamespace(get_data_dir=lambda _: Path(tmp))
    modules["astrbot.core.star.filter.event_message_type"].EventMessageType = types.SimpleNamespace(GROUP_MESSAGE=1)
    with patch.dict(sys.modules, modules):
        sys.path.insert(0, str(ROOT))
        try:
            name = "plugins.astrbot_plugin_group_activation.main"
            sys.modules.pop(name, None)
            return importlib.import_module(name).GroupActivationPlugin
        finally:
            sys.path.remove(str(ROOT))


class Event:
    def __init__(self, sender: str, text: str, *, at: bool = False, image: bool = False):
        self.sender = sender
        self.message_str = text
        self.is_at_or_wake_command = at
        self.message_obj = types.SimpleNamespace(message=[
            types.SimpleNamespace(type="image", url="base64://YQ==")
        ] if image else [])
        self.extra = {}

    def get_group_id(self): return "group-1"
    def get_sender_id(self): return self.sender
    def get_sender_name(self): return self.sender
    def get_self_id(self): return "bot"
    def set_extra(self, key, value): self.extra[key] = value
    def get_extra(self, key, default=None): return self.extra.get(key, default)


class GroupGateTests(unittest.TestCase):
    def test_listen_followup_and_image_only_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin = plugin_with_fake_astrbot(tmp)
            bot = Plugin(types.SimpleNamespace())
            bot.cfg.l3_enable = False
            bot.cfg.kb_enable = False
            bot.cfg.enable_mention = False
            bot.cfg.merge_enable = False

            async def scenario():
                ambient = Event("alice", "今天我们聊什么")
                await bot.on_group_message(ambient)
                self.assertFalse(ambient.get_extra("ga_admitted", False))
                self.assertEqual(len(bot.wm.state("group-1").l1), 1)

                at = Event("alice", "你好", at=True)
                await bot.on_group_message(at)
                self.assertTrue(at.get_extra("ga_admitted"))

                stranger = Event("bob", "那你继续解释一下？")
                await bot.on_group_message(stranger)
                self.assertFalse(stranger.get_extra("ga_admitted", False))

                image = Event("alice", "", image=True)
                await bot.on_group_message(image)
                self.assertFalse(image.get_extra("ga_admitted", False))
                self.assertIn("group-1:alice", bot._last_image)

                unrelated = Event("alice", "吃饭了")
                await bot.on_group_message(unrelated)
                self.assertFalse(unrelated.get_extra("ga_admitted", False))

                question = Event("alice", "这张图是什么？")
                await bot.on_group_message(question)
                self.assertTrue(question.get_extra("ga_admitted"))
                req = types.SimpleNamespace(
                    contexts=[{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
                    ]}], image_urls=[], extra_user_content_parts=[], system_prompt="stable-persona",
                )
                await bot.inject_memory(question, req)
                self.assertEqual(req.image_urls, ["base64://YQ=="])
                self.assertEqual(req.contexts[0]["content"][0]["type"], "text")
                self.assertEqual(req.system_prompt, "stable-persona")
                self.assertTrue(req.extra_user_content_parts[0]["_no_save"])

                another_image = Event("alice", "", image=True)
                await bot.on_group_message(another_image)
                bare_at = Event("alice", "", at=True)
                await bot.on_group_message(bare_at)
                req2 = types.SimpleNamespace(
                    contexts=[], image_urls=[], extra_user_content_parts=[], system_prompt="persona",
                )
                await bot.inject_memory(bare_at, req2)
                self.assertEqual(req2.image_urls, ["base64://YQ=="])

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
