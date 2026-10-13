"""Replay the counting failure across batch boundaries without a paid model."""
import asyncio
import tempfile
import types
import unittest
from unittest.mock import patch

from test_group_gate import Event, plugin_with_fake_astrbot


class DialogueTests(unittest.TestCase):
    def test_counting_preserves_one_to_twelve_across_batches_and_requires_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin, module = plugin_with_fake_astrbot(tmp, return_module=True)
            bot = Plugin(types.SimpleNamespace())
            bot.cfg.l3_enable = bot.cfg.kb_enable = bot.cfg.enable_mention = False
            tick = [0.0]
            clock = types.SimpleNamespace(time=lambda: 1000 + tick[0], monotonic=lambda: tick[0])

            def request():
                return types.SimpleNamespace(contexts=[], image_urls=[], prompt="",
                                             extra_user_content_parts=[], system_prompt="溟月")

            async def scenario():
                bot._directed_at['group-1:alice'] = 1000
                bot.wm.activate('group-1', 1000)
                command = Event('alice', '来吧，数数')
                await bot.on_group_message(command)
                self.assertTrue(command.get_extra('ga_admitted'))
                self.assertTrue(command.get_extra('ga_required'))
                for n in range(1, 11):
                    tick[0] = n
                    number = Event('alice', str(n))
                    await bot.on_group_message(number)
                    self.assertFalse(number.get_extra('ga_admitted', False))
                tick[0] = 15
                await bot.inject_memory(command, request())
                tick[0] = 21
                await bot.on_group_message(Event('alice', '11 12'))
                self.assertNotIn('group-1', bot._batches)
                tick[0] = 27
                question = Event('alice', '我数了多少数？')
                await bot.on_group_message(question)
                self.assertTrue(question.get_extra('ga_required'))
                tick[0] = 32
                req = request()
                await bot.inject_memory(question, req)
                self.assertFalse(question.get_extra('ga_decidable'))
                self.assertEqual(bot._number_tasks['group-1:alice'].entries,
                                 [str(n) for n in range(1, 11)] + ['11 12'])
                self.assertEqual(len(bot.wm.state('group-1').l1), 10)
                context = req.extra_user_content_parts[0]['text']
                self.assertIn('"1", "2", "3"', context)
                self.assertIn('"11 12"', context)
                self.assertNotIn('# 参与规则', context)
                self.assertIn('不能输出 [PASS]', context)

                # A mistaken PASS becomes an actual answer without a retry request.
                result = types.SimpleNamespace(chain=[types.SimpleNamespace(text='[PASS]')],
                                               get_plain_text=lambda: '[PASS]')
                question.get_result = lambda: result
                sent = []
                question.plain_result = lambda value: value
                question.set_result = sent.append
                await bot.decide_participation(question)
                self.assertEqual(sent, ['你一共数了 12 个数。'])

            with patch.object(module, 'time', clock):
                asyncio.run(scenario())

    def test_recent_request_reopens_window_but_stranger_and_stale_task_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin, module = plugin_with_fake_astrbot(tmp, return_module=True)
            bot = Plugin(types.SimpleNamespace())
            bot.cfg.l3_enable = bot.cfg.kb_enable = bot.cfg.enable_mention = False
            bot.cfg.merge_enable = False
            tick = [0.0]
            clock = types.SimpleNamespace(time=lambda: 1000 + tick[0], monotonic=lambda: tick[0])

            async def scenario():
                bot._directed_at['group-1:alice'] = 1000
                bot.wm.activate('group-1', 1000)
                tick[0] = 35  # Window expired but the conversation link is still recent.
                request = Event('alice', '请翻译这句话。')
                await bot.on_group_message(request)
                self.assertTrue(request.get_extra('ga_required'))
                stranger = Event('bob', '来吧，数数')
                await bot.on_group_message(stranger)
                self.assertFalse(stranger.get_extra('ga_admitted', False))
                await bot.on_group_message(Event('alice', '来吧，数数'))
                tick[0] = 36
                await bot.on_group_message(Event('bob', '99'))
                self.assertEqual(bot._number_tasks['group-1:alice'].entries, [])
                tick[0] = 156
                await bot.on_group_message(Event('alice', '7'))
                self.assertNotIn('group-1:alice', bot._number_tasks)

            with patch.object(module, 'time', clock):
                asyncio.run(scenario())

    def test_buffer_limits_are_explicit_and_sender_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, module = plugin_with_fake_astrbot(tmp, return_module=True)
            task = module.NumberTask('数数', 0)
            task.append('1', 1, 2, 20)
            task.append('2', 2, 2, 20)
            task.append('3', 3, 2, 20)
            self.assertEqual(task.entries, ['1', '2'])
            self.assertTrue(task.truncated)


if __name__ == '__main__':
    unittest.main()
