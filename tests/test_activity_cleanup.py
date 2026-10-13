"""Regression cases for memory timers and reset while requests are in flight."""
import asyncio
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_group_gate import Event, plugin_with_fake_astrbot


class Chain:
    def message(self, text):
        self.text = text
        return self


class ActivityTests(unittest.TestCase):
    def test_failed_send_does_not_extend_window_and_cancelled_send_never_reaches_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin, module = plugin_with_fake_astrbot(tmp, return_module=True)
            bot = Plugin(types.SimpleNamespace())
            bot.cfg.l3_enable = bot.cfg.kb_enable = False

            async def scenario():
                event = Event('alice', 'hello', at=True)
                adapter = AsyncMock(side_effect=RuntimeError('OneBot failed'))
                event.send = adapter
                await bot.on_group_message(event)
                with self.assertRaises(RuntimeError):
                    await event.send(Chain())
                bot.wm.sleep('group-1')
                await bot.extend_after_reply(event)
                self.assertFalse(bot.wm.is_active('group-1'))
                self.assertFalse(event.get_extra('ga_send_success'))
                bot._cancel_group_activity('group-1')
                with self.assertRaises(asyncio.CancelledError):
                    await event.send(Chain())
                self.assertEqual(adapter.await_count, 1)

            asyncio.run(scenario())

    def test_memory_timer_rechecks_extended_window_and_reply_does_not_count_as_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin, module = plugin_with_fake_astrbot(tmp, return_module=True)
            bot = Plugin(types.SimpleNamespace())
            delays, extracted = [], []

            async def sleep(seconds):
                delays.append(seconds)

            async def extract(gid):
                extracted.append(gid)

            async def scenario():
                bot._l3_extract = extract
                with patch.object(module, 'asyncio', types.SimpleNamespace(sleep=sleep)), \
                        patch.object(bot.wm, 'remaining', side_effect=[30, 10]), \
                        patch.object(bot.wm, 'is_active', side_effect=[True, False]):
                    await bot._l3_extract_later('group-1')
                self.assertEqual(delays, [32, 12])
                self.assertEqual(extracted, ['group-1'])
                bot._l3_seen['group-1'] = 4
                event = Event('alice', 'hello')
                event.set_extra('ga_admitted', True)
                with patch.object(bot, '_schedule_l3') as schedule:
                    await bot.extend_after_reply(event)
                    schedule.assert_called_once_with('group-1', count_message=False)
                self.assertEqual(bot._l3_seen['group-1'], 4)

            asyncio.run(scenario())

    def test_clear_cancels_waiting_batch_and_late_results_without_touching_other_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin, module = plugin_with_fake_astrbot(tmp, return_module=True)
            bot = Plugin(types.SimpleNamespace())
            bot.cfg.l3_enable = bot.cfg.kb_enable = False
            gate = asyncio.Event()
            entered = asyncio.Event()

            async def wait(seconds):
                entered.set()
                await gate.wait()

            async def scenario():
                first = Event('alice', '来吧，数数', at=True)
                first.stop_event = Mock()
                await bot.on_group_message(first)
                other = Event('bob', '另一个问题', at=True)
                other.get_group_id = lambda: 'group-2'
                await bot.on_group_message(other)
                bot._last_image['group-1:alice'] = (['image'], 0)
                prefetch = asyncio.create_task(asyncio.Event().wait())
                bot._kb_prefetch['group-1'] = ('query', prefetch)
                req = types.SimpleNamespace(contexts=[], image_urls=[], prompt='',
                                            extra_user_content_parts=[], system_prompt='persona')
                with patch.object(module, 'asyncio', types.SimpleNamespace(sleep=wait, CancelledError=asyncio.CancelledError)):
                    pending = asyncio.create_task(bot.inject_memory(first, req))
                    await entered.wait()
                    command = Event('alice', '/gaclear')
                    command.send = AsyncMock()
                    with patch.object(module, 'MessageChain', Chain):
                        await bot.gaclear(command)
                    gate.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                await asyncio.sleep(0)
                self.assertTrue(prefetch.cancelled())
                self.assertEqual(req.extra_user_content_parts, [])
                first.stop_event.assert_called_once()
                self.assertNotIn('group-1', bot._batches)
                self.assertIn('group-2', bot._batches)
                self.assertNotIn('group-1:alice', bot._number_tasks)
                self.assertNotIn('group-1:alice', bot._last_image)
                self.assertNotIn('group-1:alice', bot._directed_at)
                self.assertEqual(len(bot.wm.state('group-1').l1), 0)
                cleared = []
                first.clear_result = lambda: cleared.append(True)
                await bot.decide_participation(first)
                await bot.fix_mentions(first)
                await bot.extend_after_reply(first)
                self.assertEqual(len(cleared), 2)
                self.assertFalse(bot.wm.is_active('group-1'))
                self.assertTrue(bot._event_is_current(other))

            asyncio.run(scenario())

    def test_forget_cannot_be_undone_by_extraction_returning_after_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin, module = plugin_with_fake_astrbot(tmp, return_module=True)
            entered, release = asyncio.Event(), asyncio.Event()

            async def chat(**kwargs):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    # Simulate a provider that finishes its work despite cancellation.
                    await release.wait()
                return types.SimpleNamespace(completion_text='{"facts":[{"member":"alice","fact":"喜欢红茶"}]}')

            provider = types.SimpleNamespace(text_chat=chat)
            bot = Plugin(types.SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider)))
            bot._l3_seen['group-1'] = 5
            bot.wm.push_l1('group-1', {'user': 'alice', 'text': '喜欢红茶', 'ts': 0})
            bot.ltm.add_facts('group-1', 'alice', ['旧事实'])

            async def scenario():
                extraction = asyncio.create_task(bot._l3_extract('group-1'))
                bot._l3_tasks['group-1'] = extraction
                await entered.wait()
                command = Event('alice', '/gaforget')
                command.send = AsyncMock()
                with patch.object(module, 'MessageChain', Chain):
                    forgetting = asyncio.create_task(bot.gaforget(command))
                    await asyncio.sleep(0)
                    release.set()
                    await forgetting
                await extraction
                self.assertEqual(bot.ltm.total_facts('group-1'), 0)
                self.assertEqual(bot._l3_seen['group-1'], 0)

            asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
