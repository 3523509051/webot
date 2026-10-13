"""Offline search authorization, quota and bounded KB regression tests."""
import asyncio
import json
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_group_gate import Event, plugin_with_fake_astrbot


class SearchKBTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Plugin, self.module = plugin_with_fake_astrbot(self.tmp.name, return_module=True)
        self.bot = Plugin(types.SimpleNamespace())
        self.bot.cfg.l3_enable = self.bot.cfg.kb_enable = self.bot.cfg.enable_mention = False
        self.bot.cfg.search_enable = True
        self.bot._baidu_search_key = lambda: "fake-offline-key"
        self.calls = []

    async def asyncTearDown(self):
        tasks = [entry[1] for entry in self.bot._kb_prefetch.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tmp.cleanup()

    def item(self, event, label):
        event.message_str = label
        return self.bot._batch_item(event, label, event.is_at_or_wake_command, 0)

    def http(self, status=200, data=None, entered=None, release=None):
        owner = self
        class Response:
            async def __aenter__(self):
                if entered:
                    entered.set()
                if release:
                    await release.wait()
                return self
            async def __aexit__(self, *args): pass
            async def json(self): return data if data is not None else {}
        Response.status = status
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def post(self, url, **kwargs):
                owner.calls.append(kwargs["json"])
                return Response()
        return patch.dict(sys.modules, {"aiohttp": types.SimpleNamespace(
            ClientSession=Session, ClientTimeout=lambda **kwargs: None)})

    async def test_intent_rejects_quotes_capabilities_and_negatives(self):
        parse = self.module.search_intent
        for text in ("你好", "你能搜索吗？", "搜索是什么意思？", "他说“帮我搜索天气”"):
            self.assertIsNone(parse(text), text)
        for text in ("不要搜索", "不用联网搜索了", "取消搜索"):
            self.assertIs(parse(text), False, text)
        for text in ("请百度搜索天气", "帮我搜索北京天气", "上网查一下新闻"):
            self.assertIs(parse(text), True, text)
        with self.http():
            result = await self.bot.web_search(Event("a", "你好"), "天气")
        self.assertIn("没有明确", result)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.bot._search_quota.path.exists())

    async def test_batch_permissions_bind_sender_and_allow_withdrawal(self):
        root = Event("alice", "解释排序", at=True)
        items = [self.item(root, "解释排序"),
                 self.item(Event("bob", "请百度搜索天气"), "请百度搜索天气")]
        self.bot._prepare_request_metadata(root, items)
        with self.http():
            self.assertIn("未授权", await self.bot.web_search(root, "天气", "alice"))
            self.assertIn("没有返回结果", await self.bot.web_search(root, "天气", "bob"))
            await self.bot.on_group_message(Event("bob", "不要搜索了"))
            self.assertIn("撤回", await self.bot.web_search(root, "天气", "bob"))
        self.assertEqual(len(self.calls), 1)
        items.append(self.item(Event("bob", "不要搜索"), "不要搜索"))
        self.bot._prepare_request_metadata(root, items)
        self.assertEqual(root.get_extra("ga_search_requests"), [])

    async def test_multiple_authorizations_require_explicit_sender(self):
        event = Event("a", "请搜索天气")
        self.bot._prepare_request_metadata(event, [
            self.item(Event(sid, "请搜索天气"), "请搜索天气") for sid in ("a", "b")])
        with self.http():
            result = await self.bot.web_search(event, "天气")
        self.assertIn("不明确", result)
        self.assertEqual(self.calls, [])

    async def test_concurrent_requests_reserve_before_network(self):
        self.bot.cfg.search_daily_limit = 1
        entered, release = asyncio.Event(), asyncio.Event()
        with self.http(data={"references": [{"url": "https://example.test", "title": "test"}]},
                       entered=entered, release=release):
            first = asyncio.create_task(self.bot.web_search(Event("a", "请搜索天气"), "天气"))
            await asyncio.wait_for(entered.wait(), 1)
            second = await self.bot.web_search(Event("b", "请搜索新闻"), "新闻")
            release.set()
            await first
        self.assertIn("已用完", second)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.bot._search_quota._read()["success"], 1)

    async def test_failures_and_empty_results_consume_allowance(self):
        event = Event("a", "请搜索天气")
        with self.http(status=500):
            self.assertIn("失败", await self.bot.web_search(event, "天气"))
        with self.http():
            self.assertIn("没有返回结果", await self.bot.web_search(event, "天气"))
        stats = self.bot._search_quota._read()
        self.assertEqual((stats["count"], stats["failed"], stats["empty"]), (2, 1, 1))

    async def test_corruption_and_write_failure_prevent_requests(self):
        quota = self.bot._search_quota
        quota.path.write_text("broken", encoding="utf-8")
        with self.http():
            self.assertIn("不可用", await self.bot.web_search(Event("a", "请搜索天气"), "天气"))
        quota.path.unlink()
        with patch.object(quota, "_write", side_effect=OSError("disk unavailable")), self.http():
            self.assertIn("不可用", await self.bot.web_search(Event("a", "请搜索天气"), "天气"))
        self.assertEqual(self.calls, [])

    async def test_quota_migrates_old_file_and_handles_midnight(self):
        quota = self.bot._search_quota
        quota.day = lambda: "2026-10-13"
        quota.path.write_text(json.dumps({"date": quota.day(), "count": 2}), encoding="utf-8")
        ticket = await quota.reserve(3)
        self.assertIsNone(await quota.reserve(3))
        quota.day = lambda: "2026-10-14"
        await quota.finish(ticket, "success")
        self.assertEqual(quota.remaining(3), 3)
        await quota.reserve(3)
        self.assertEqual(quota._read()["count"], 1)

    async def test_cancelled_request_keeps_reservation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        with self.http(entered=entered, release=release):
            task = asyncio.create_task(self.bot.web_search(Event("a", "请搜索天气"), "天气"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
        stats = self.bot._search_quota._read()
        self.assertEqual((stats["count"], stats["failed"]), (1, 1))

    async def test_kb_query_uses_plain_batch_text_and_reuses_prefetch(self):
        self.bot.cfg.kb_enable = True
        self.bot.kbs.search = AsyncMock(return_value="knowledge")
        event = Event("alice", "解释排序")
        items = [self.item(event, "[At:123] 解释排序"),
                 self.item(Event("b", "示例代码"), "示例代码"),
                 self.item(Event("b", "12"), "12")]
        self.bot._prepare_request_metadata(event, items)
        query = event.get_extra("ga_kb_query")
        self.assertEqual(query, "解释排序\n示例代码")
        prefetch = self.bot._start_kb_prefetch("group-1", query)
        self.assertEqual(await self.bot._kb_search_for("group-1", query, prefetch), "knowledge")
        self.bot.kbs.search.assert_awaited_once_with("group-1", query)
        self.assertLessEqual(len(self.module.kb_query(["x" * 2000], 100)), 100)

    async def test_kb_total_timeout_does_not_retry(self):
        self.bot.cfg.kb_enable = True
        self.bot.cfg.kb_retrieval_timeout = .05
        async def slow(*args):
            await asyncio.sleep(10)
        self.bot.kbs.search = AsyncMock(side_effect=slow)
        prefetch = self.bot._start_kb_prefetch("group-1", "query")
        self.assertEqual(await asyncio.wait_for(self.bot._kb_search_for("group-1", "query", prefetch), .5), "")
        self.bot.kbs.search.assert_awaited_once()

    async def test_real_batch_refreshes_query_and_freezes_sender_authorization(self):
        self.bot.cfg.kb_enable = True
        self.bot.kbs.search = AsyncMock(return_value="knowledge")
        tick = [0.0]
        clock = types.SimpleNamespace(time=lambda: 1000 + tick[0], monotonic=lambda: tick[0])
        with patch.object(self.module, "time", clock):
            root = Event("alice", "解释排序", at=True)
            await self.bot.on_group_message(root)
            old = root.get_extra("ga_kb_prefetch")
            tick[0] = 1
            await self.bot.on_group_message(Event("bob", "请百度搜索天气", at=True))
            tick[0] = 6
            req = types.SimpleNamespace(prompt="解释排序", image_urls=[])
            await self.bot._finish_batch(root, req, root.get_extra("ga_batch"))
        self.assertEqual(root.get_extra("ga_kb_query"), "解释排序\n请百度搜索天气")
        self.assertEqual([item["sender_id"] for item in root.get_extra("ga_search_requests")], ["bob"])
        entry = root.get_extra("ga_kb_prefetch")
        self.assertEqual(await self.bot._kb_search_for("group-1", root.get_extra("ga_kb_query"), entry), "knowledge")
        await asyncio.gather(old[1], return_exceptions=True)
        self.bot.kbs.search.assert_awaited_once_with("group-1", "解释排序\n请百度搜索天气")

    async def test_missing_kb_returns_empty_without_creating_library(self):
        self.bot.cfg.kb_enable = True
        manager = types.SimpleNamespace(get_kb_by_name=AsyncMock(return_value=None), create_kb=AsyncMock())
        self.bot.kbs.context = types.SimpleNamespace(kb_manager=manager)
        self.assertEqual(await self.bot._kb_search_for("group-1", "query"), "")
        manager.create_kb.assert_not_awaited()

    async def test_newer_prefetch_is_not_removed_by_old_batch(self):
        self.bot.cfg.kb_enable = True
        self.bot.kbs.search = AsyncMock(return_value="knowledge")
        old = self.bot._start_kb_prefetch("group-1", "old")
        await old[1]
        newer = self.bot._start_kb_prefetch("group-1", "new")
        self.assertEqual(await self.bot._kb_search_for("group-1", "old", old), "knowledge")
        self.assertIs(self.bot._kb_prefetch["group-1"], newer)
        await newer[1]

    async def test_kb_supersession_and_parent_cancellation(self):
        self.bot.cfg.kb_enable = True
        async def slow(*args):
            await asyncio.sleep(10)
        self.bot.kbs.search = AsyncMock(side_effect=slow)
        old = self.bot._start_kb_prefetch("group-1", "old")
        await asyncio.sleep(0)
        newer = self.bot._start_kb_prefetch("group-1", "new")
        self.assertEqual(await self.bot._kb_search_for("group-1", "old", old), "")
        waiter = asyncio.create_task(self.bot._kb_search_for("group-1", "new", newer))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter


if __name__ == "__main__":
    unittest.main()
