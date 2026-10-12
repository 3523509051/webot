"""Verify batch timing and one admitted request across multiple senders."""
import asyncio
import json
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from test_group_gate import Event, plugin_with_fake_astrbot


class BatchTests(unittest.TestCase):
    def test_multi_sender_quiet_deadline_and_single_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin, module = plugin_with_fake_astrbot(tmp, return_module=True)
            bot = Plugin(types.SimpleNamespace())
            bot.cfg.l3_enable = bot.cfg.kb_enable = bot.cfg.enable_mention = False
            tick = [0.0]
            clock = types.SimpleNamespace(time=lambda: 1000 + tick[0], monotonic=lambda: tick[0])

            async def scenario():
                first = Event("alice", "解释一下排序算法。", at=True)
                await bot.on_group_message(first)
                batch = first.get_extra("ga_batch")
                self.assertEqual(batch.deadline, 5)
                tick[0] = 3
                second = Event("bob", "我想看 Python 示例。", at=True)
                await bot.on_group_message(second)
                self.assertFalse(second.is_at_or_wake_command)
                self.assertFalse(second.get_extra("ga_admitted", False))
                self.assertEqual(batch.deadline, 8)
                tick[0] = 4
                noise = Event("carol", "晚饭吃什么？")
                await bot.on_group_message(noise)
                self.assertEqual(batch.deadline, 8)
                self.assertNotIn("carol", batch.participants)
                tick[0] = 6
                supplement = Event("alice", "先讲最简单的版本，不要递归。")
                await bot.on_group_message(supplement)
                self.assertEqual(batch.deadline, 11)
                self.assertFalse(supplement.is_at_or_wake_command)

                async def advance(seconds):
                    tick[0] += seconds

                req = types.SimpleNamespace(contexts=[], image_urls=[], prompt="解释一下排序算法。",
                                            extra_user_content_parts=[], system_prompt="溟月")
                with patch.object(module, "asyncio", types.SimpleNamespace(sleep=advance)):
                    await bot.inject_memory(first, req)
                self.assertEqual(tick[0], 11)
                self.assertEqual(len(batch.items), 3)
                self.assertIn("bob", req.prompt)
                self.assertIn("不要递归", req.prompt)
                self.assertNotIn("晚饭吃什么", req.prompt)
                self.assertEqual(req.system_prompt, "溟月")
                self.assertEqual(len(req.extra_user_content_parts), 1)
                self.assertNotIn("group-1", bot._batches)
                tick[0] = 12
                next_event = Event("bob", "新的问题", at=True)
                await bot.on_group_message(next_event)
                self.assertTrue(next_event.get_extra("ga_admitted"))
                self.assertIsNot(next_event.get_extra("ga_batch"), batch)

            with patch.object(module, "time", clock):
                asyncio.run(scenario())

    def test_hard_cap_does_not_slide_past_thirty_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin, module = plugin_with_fake_astrbot(tmp, return_module=True)
            Batch = module.MessageBatch
            batch = Batch(0, 5, 30)
            for tick in range(0, 29, 4):
                self.assertTrue(batch.collecting(tick))
                batch.add({"sender_id": "alice"}, tick)
            self.assertEqual(batch.deadline, 30)
            self.assertFalse(batch.collecting(30))

    def test_images_keep_sender_ownership_and_do_not_extend_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin, module = plugin_with_fake_astrbot(tmp, return_module=True)
            bot = Plugin(types.SimpleNamespace())
            bot.cfg.l3_enable = bot.cfg.kb_enable = bot.cfg.enable_mention = False
            tick = [0.0]
            clock = types.SimpleNamespace(time=lambda: 1000 + tick[0], monotonic=lambda: tick[0])

            async def scenario():
                first = Event("alice", "这张图是什么？", at=True)
                await bot.on_group_message(first)
                batch = first.get_extra("ga_batch")
                tick[0] = 2
                await bot.on_group_message(Event("alice", "", image=True))
                self.assertEqual(batch.deadline, 5)
                image_b = Event("bob", "", image=True)
                image_b.message_obj.message[0].url = "base64://Yg=="
                await bot.on_group_message(image_b)
                await bot.on_group_message(Event("bob", "这张图里写什么？", at=True))
                tick[0] = 7
                req = types.SimpleNamespace(image_urls=[], prompt="")
                await bot._finish_batch(first, req, batch)
                records = json.loads(req.prompt.split("JSON 是用户消息数据：\n", 1)[1])
                self.assertEqual(req.image_urls, ["base64://YQ==", "base64://Yg=="])
                self.assertEqual(records[0]["该发送者附图序号"], [1])
                self.assertEqual(records[1]["该发送者附图序号"], [2])
                self.assertEqual(bot._last_image, {})

            with patch.object(module, "time", clock):
                asyncio.run(scenario())

    def test_cancelled_owner_cleans_only_its_own_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin, module = plugin_with_fake_astrbot(tmp, return_module=True)
            bot = Plugin(types.SimpleNamespace())
            batch = module.MessageBatch(module.time.monotonic(), 5, 30)
            batch.add({"sender_id": "alice"}, batch.started)
            replacement = module.MessageBatch(batch.started + 30, 5, 30)
            bot._batches["group-1"] = replacement

            async def cancel_sleep(seconds):
                raise asyncio.CancelledError()

            async def scenario():
                with patch.object(module, "asyncio", types.SimpleNamespace(sleep=cancel_sleep)):
                    with self.assertRaises(asyncio.CancelledError):
                        await bot._finish_batch(Event("alice", "hi"), None, batch)
                self.assertTrue(batch.closed)
                self.assertIs(bot._batches["group-1"], replacement)

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
