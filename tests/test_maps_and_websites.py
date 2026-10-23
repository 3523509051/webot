"""No real maps, web, LLM or desktop requests in these regression tests."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_group_gate import Event, plugin_with_fake_astrbot
from plugins.astrbot_plugin_group_activation.web_policy import (
    authorized_urls, check_domain, clean_url, public_addresses, should_skip_ping,
    strip_leading_verb)
from plugins.astrbot_plugin_group_activation.amap import AMapClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import safe_browser
sys.path.pop(0)


class Chain:
    def __init__(self, text=""): self.text = text
    def message(self, text): return Chain(text)
    def get_plain_text(self): return self.text


class PolicyTests(unittest.TestCase):
    def test_domain_boundaries_and_https_only(self):
        self.assertEqual(check_domain("https://zh.wikipedia.org/wiki/Test", ["wikipedia.org"]),
                         "https://zh.wikipedia.org/wiki/Test")
        for url in ("https://github.com.evil.test", "https://evilgithub.com",
                    "https://github.com:444", "https://me:secret@github.com", "file:///etc/passwd",
                    "https://127.0.0.1", "https://github.com\\@evil.test"):
            with self.assertRaises(ValueError, msg=url):
                check_domain(url, ["github.com"])

    def test_any_private_dns_address_blocks_entire_target(self):
        for ips in (["127.0.0.1"], ["8.8.8.8", "10.1.2.3"], ["::1"], ["169.254.169.254"], ["224.0.0.1"], []):
            with self.assertRaises(ValueError): public_addresses(ips)
        self.assertEqual(public_addresses(["8.8.8.8"]), ["8.8.8.8"])

    def test_proxy_fake_ip_is_allowed_only_when_enabled(self):
        """代理 fake-ip（198.18.0.0/15）默认仍拒绝，显式开启后放行；混入真实内网地址时依旧拒绝。"""
        for ips in (["198.18.0.93"], ["198.19.0.1"]):
            with self.assertRaises(ValueError): public_addresses(ips)
            self.assertEqual(public_addresses(ips, allow_fake_ip=True), ips)
        with self.assertRaises(ValueError):
            public_addresses(["198.18.0.93", "10.0.0.1"], allow_fake_ip=True)
        with self.assertRaises(ValueError):
            public_addresses(["198.18.0.93", "127.0.0.1"], allow_fake_ip=True)

    def test_private_ips_allowed_only_when_fully_open(self):
        """allow_private_ips=True 表示完全放开（本机/内网/保留地址都读）；默认仍拒绝。"""
        for ips in (["127.0.0.1"], ["192.168.1.5"], ["10.0.0.1"], ["::1"], ["169.254.169.254"]):
            with self.assertRaises(ValueError): public_addresses(ips)
            self.assertEqual(public_addresses(ips, allow_private_ips=True), ips)
        with self.assertRaises(ValueError): public_addresses([])

    def test_leading_verb_is_stripped_and_chinese_domain_is_encoded(self):
        """`搜索xxx.com` 这类把动词并进网址的输入要先剥掉动词，中文域名再自动转码。"""
        for raw, want in (("搜索深圳奶蛙大学.com", "深圳奶蛙大学.com"),
                          ("搜一下 bilibili.com", "bilibili.com"),
                          ("查一下 北京天气", "北京天气"),
                          ("打开https://github.com/x", "https://github.com/x"),
                          ("https://github.com/x", "https://github.com/x")):
            self.assertEqual(strip_leading_verb(raw), want)
        self.assertEqual(clean_url(strip_leading_verb("搜索深圳奶蛙大学.com")),
                         "http://" + "深圳奶蛙大学.com".encode("idna").decode() + "/")

    def test_ping_gate_skipped_for_fake_and_private_addresses(self):
        """公网地址仍受延迟门槛约束；本机/内网/代理地址跳过（否则放开地址后仍会被卡死）。"""
        self.assertFalse(should_skip_ping("8.8.8.8"))
        self.assertFalse(should_skip_ping("198.18.0.93"))
        self.assertTrue(should_skip_ping("198.18.0.93", allow_fake_ip=True))
        self.assertFalse(should_skip_ping("192.168.1.5"))
        self.assertTrue(should_skip_ping("192.168.1.5", allow_private_ips=True))
        self.assertFalse(should_skip_ping("not-an-ip", allow_private_ips=True))

    def test_url_grants_are_explicit_and_quotes_do_not_grant(self):
        url = "https://github.com/test/repo"
        self.assertEqual(authorized_urls("请访问 " + url), [url])
        for text in ("不要访问 " + url, "有人说“访问 " + url + "”"):
            self.assertEqual(authorized_urls(text), [])
        self.assertEqual(authorized_urls("这是 " + url), [url])
        self.assertEqual(authorized_urls(url), [url])
        self.assertEqual(clean_url("https://GITHUB.com#x"), "https://github.com/")

    def test_bare_chinese_domains_and_http_are_supported(self):
        expected = "http://" + "深圳奶蛙大学.com".encode("idna").decode("ascii") + "/"
        self.assertEqual(authorized_urls("深圳奶蛙大学.com"), [expected])
        self.assertEqual(authorized_urls("http://深圳奶蛙大学.com"), [expected])
        self.assertEqual(authorized_urls("请访问深圳奶蛙大学.com"), [expected])
        self.assertEqual(authorized_urls("www.sztu.edu.cn"), ["http://www.sztu.edu.cn/"])
        self.assertEqual(authorized_urls("http://example.org"), ["http://example.org/"])
        self.assertEqual(authorized_urls("email@example.org"), [])

    def test_html_removes_executable_and_embedded_content(self):
        parser = safe_browser.TextParser()
        parser.feed('<h1>旅行</h1><script>steal()</script><iframe>bad</iframe><p>西湖</p><img src="evil">')
        self.assertIn("旅行", parser.text())
        self.assertIn("西湖", parser.text())
        self.assertNotIn("steal", parser.text())
        self.assertNotIn("bad", parser.text())

    def test_article_content_is_preferred_to_navigation(self):
        parser = safe_browser.TextParser()
        parser.feed("<nav>navigation</nav><main><p>menu</p><article><p>travel guide</p></article></main>")
        self.assertEqual(parser.text(), "travel guide")

    def test_link_plus_metadata_question_grants_clean_url(self):
        url = "https://www.bilibili.com/video/BV1tQ4y1E7qb?vd_source=abc123"
        self.assertEqual(authorized_urls(url + "这个视频的播放量是多少？"), [url])
        self.assertEqual(authorized_urls(url + "\n这个视频的简介是什么？"), [url])
        self.assertEqual(authorized_urls("请总结这个页面 " + url), [url])
        self.assertEqual(authorized_urls(url), [url])
        self.assertEqual(authorized_urls("https://github.com/访问"), ["https://github.com/访问"])
        wiki = "https://zh.wikipedia.org/wiki/杭州"
        self.assertEqual(authorized_urls("请阅读 " + wiki), [wiki])


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Plugin, self.module = plugin_with_fake_astrbot(self.tmp.name, return_module=True)
        self.provider = types.SimpleNamespace(text_chat=AsyncMock(return_value=types.SimpleNamespace(completion_text='{"safe":true}')))
        self.bot = Plugin(types.SimpleNamespace(get_using_provider_async=AsyncMock(return_value=self.provider)))
        self.bot.cfg.l3_enable = self.bot.cfg.kb_enable = self.bot.cfg.enable_mention = False
    def tearDown(self): self.tmp.cleanup()

    def prepare(self, sender="a", text="请访问 https://github.com/test", mentioned=True):
        event = Event(sender, text, at=mentioned)
        item = self.bot._batch_item(event, text, mentioned, 0)
        self.bot._prepare_request_metadata(event, [item])
        event.bot = types.SimpleNamespace(call_action=AsyncMock(return_value={
            "ok": True, "url": "https://github.com/test", "ping_ms": 20, "text": "旅行说明"}))
        return event

    async def test_website_can_read_found_urls_without_batch_membership(self):
        event = self.prepare(mentioned=False)
        self.assertIn("旅行说明", await self.bot.read_website(event, "https://github.com/test", "a"))
        event = self.prepare()
        self.assertIn("旅行说明", await self.bot.read_website(event, "https://github.com/test", "b"))
        self.assertIn("旅行说明", await self.bot.read_website(event, "https://github.com/other"))
        self.assertIn("旅行说明", await self.bot.read_website(event, "https://github.com/test", "a"))
        self.assertEqual(event.bot.call_action.await_count, 3)

    async def test_no_url_in_current_message_is_not_an_access_denial(self):
        event = self.prepare(text="找快手官网并介绍首页内容")
        self.assertEqual(event.get_extra("ga_website_requests"), [])
        self.assertIn("旅行说明", await self.bot.read_website(event, "https://www.kuaishou.com/"))

    async def test_prompt_does_not_reintroduce_empty_batch_permission_gate(self):
        self.bot.cfg.merge_enable = False
        event = Event("a", "找快手官网并介绍首页内容", at=True)
        await self.bot.on_group_message(event)
        req = types.SimpleNamespace(prompt=event.message_str, contexts=[], image_urls=[],
                                    extra_user_content_parts=[], system_prompt="persona")
        await self.bot.inject_memory(event, req)
        prompt = str(req.extra_user_content_parts)
        self.assertIn("始终可用", prompt)
        self.assertNotIn("本批次没有联网搜索授权", prompt)
        self.assertNotIn("只能读取本次明确授权", prompt)

    async def test_only_mentioned_participant_can_grant_in_multi_sender_batch(self):
        root = Event("a", "解释排序", at=True)
        events = [root, Event("b", "访问 https://github.com/test", at=True),
                  Event("c", "访问 https://github.com/other")]
        self.bot._prepare_request_metadata(root, [self.bot._batch_item(e, e.message_str, e.is_at_or_wake_command, 0) for e in events])
        self.assertEqual(root.get_extra("ga_website_requests"), [{"sender_id": "b", "urls": ["https://github.com/test"], "permission_version": 0}])

    async def test_withdrawal_revokes_old_website_request(self):
        event = self.prepare()
        await self.bot.on_group_message(Event("a", "不要访问了"))
        self.assertIn("撤回", await self.bot.read_website(event, "https://github.com/test", "a"))
        event.bot.call_action.assert_not_awaited()

    async def test_unsafe_page_and_unavailable_moderator_never_return_original(self):
        event = self.prepare()
        event.bot.call_action.return_value["text"] = "血腥细节"
        result = await self.bot.read_website(event, "https://github.com/test", "a")
        self.assertIn("拦截", result)
        self.assertNotIn("血腥细节", result)
        self.provider.text_chat.assert_not_awaited()
        self.provider.text_chat.side_effect = TimeoutError()
        event.bot.call_action.return_value["text"] = "普通内容"
        self.assertIn("拦截", await self.bot.read_website(event, "https://github.com/test", "a"))

    async def test_moderator_malformed_and_numeric_true_are_rejected(self):
        for verdict in ('{"safe":1}', 'safe', '{"safe":true,"extra":1}', '{"safe":false}'):
            self.provider.text_chat.return_value.completion_text = verdict
            self.assertFalse(await self.bot._moderate_web_text("普通内容"))

    async def test_epoch_change_during_page_read_discards_result(self):
        event = self.prepare()
        event.set_extra("ga_admitted", True)
        event.set_extra("ga_group_version", 0)
        async def read(*args, **kwargs):
            self.bot._group_versions["group-1"] = 1
            return {"ok": True, "text": "private late content"}
        event.bot.call_action.side_effect = read
        self.assertIn("已取消", await self.bot.read_website(event, "https://github.com/test", "a"))
        self.provider.text_chat.assert_not_awaited()

    async def test_final_reply_is_filtered_and_stream_is_buffered(self):
        event = self.prepare()
        sent = []
        async def send(message): sent.append(message.get_plain_text())
        event.send = send
        event.send_streaming = AsyncMock()
        self.bot._trace_delivery(event)
        event.set_extra("ga_website_used", True)
        with patch.object(self.module, "MessageChain", Chain):
            await event.send(Chain("血腥细节"))
            self.assertEqual(sent, ["网页相关回复未通过内容检查，已拦截。"])
            async def stream():
                yield Chain("普通")
                self.assertEqual(len(sent), 1)  # no early output
                yield Chain("内容")
            await event.send_streaming(stream())
        self.assertEqual(sent[-1], "普通内容")
        self.assertTrue(event.get_extra("ga_send_success"))

    async def test_map_authorization_and_missing_key(self):
        event = self.prepare(text="请安排杭州旅游路线")
        with patch.dict("os.environ", {"AMAP_WEB_KEY": ""}):
            self.assertIn("Key", await self.bot.amap_places(event, "杭州", "西湖", "a"))
            self.assertIn("没有", await self.bot.amap_places(event, "杭州", "西湖", "b"))
        self.assertFalse(self.bot._amap_quota.path.exists())

    async def test_amap_route_preserves_order_and_handles_ambiguity(self):
        client = AMapClient("fake", self.bot._amap_quota)
        async def request(path, **params):
            if path == "place/text":
                name = params["keywords"]
                return {"pois": [{"name": name, "location": "120.1,30.1", "id": name}]}
            return {"route": {"paths": [{"distance": "1500", "duration": "900", "steps": [{"instruction": "向前走"}]}]}}
        client.request = AsyncMock(side_effect=request)
        result = await client.route("杭州", ["东站", "西湖", "酒店"], "walking")
        self.assertEqual([(leg["from"], leg["to"]) for leg in result["legs"]], [("东站", "西湖"), ("西湖", "酒店")])
        self.assertEqual(client.request.await_count, 5)
        client.places = AsyncMock(return_value=[{"name": "西湖一", "location": "120,30"}, {"name": "西湖二", "location": "120,30"}])
        with self.assertRaisesRegex(ValueError, "不明确"):
            await client.location("西湖", "杭州")
        with self.assertRaises(ValueError): await client.route("杭州", ["一"], "walking")

    async def test_amap_invalid_coordinates_do_not_query_route(self):
        client = AMapClient("fake", self.bot._amap_quota)
        client.location = AsyncMock(return_value={"name": "站", "location": "nan,30"})
        client.request = AsyncMock()
        with self.assertRaisesRegex(ValueError, "无效坐标"):
            await client.route("杭州", ["一", "二"])
        client.request.assert_not_awaited()

    async def test_amap_http_error_consumes_quota_without_exposing_key(self):
        class Response:
            status = 403
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, *args, **kwargs): return Response()
        with patch.dict(sys.modules, {"aiohttp": types.SimpleNamespace(ClientSession=Session, ClientTimeout=lambda **k: None)}):
            client = AMapClient("secret-must-not-appear", self.bot._amap_quota, daily_limit=1)
            with self.assertRaisesRegex(ValueError, "HTTP 403") as exc:
                await client.places("杭州", "西湖")
            self.assertNotIn(client.key, str(exc.exception))
            with self.assertRaisesRegex(ValueError, "额度"):
                await client.places("杭州", "西湖")
        self.assertEqual(self.bot._amap_quota._read()["failed"], 1)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    def http(self, responses, calls):
        class Response:
            def __init__(self, data):
                self.status = data.get("status", 200)
                self.headers = data.get("headers", {})
                self.content_type = data.get("mime", "text/html")
                self.content_length = None
                self.charset = "utf-8"
                self.body = data.get("body", b"<p>safe</p>")
                self.content = self
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def iter_chunked(self, size):
                yield self.body
        class Session:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, url, **kwargs):
                calls.append(url)
                return Response(responses.pop(0))
        module = types.SimpleNamespace(ClientSession=Session, ClientTimeout=lambda **kwargs: None,
            TCPConnector=lambda **kwargs: None, DummyCookieJar=lambda: None,
            abc=types.SimpleNamespace(AbstractResolver=object))
        return patch.dict(sys.modules, {"aiohttp": module})

    async def test_disallowed_redirect_is_stopped_before_next_request(self):
        calls = []
        with self.http([{"status": 302, "headers": {"Location": "https://evil.test/"}}], calls), \
             patch.object(safe_browser, "resolve", AsyncMock(return_value=["8.8.8.8"])), \
             patch.object(safe_browser, "ping_ms", AsyncMock(return_value=20)):
            with self.assertRaisesRegex(ValueError, "白名单"):
                await safe_browser.fetch_document("https://github.com/", ["github.com"])
        self.assertEqual(len(calls), 1)

    async def test_slow_ping_does_not_fetch(self):
        calls = []
        with self.http([], calls), patch.object(safe_browser, "resolve", AsyncMock(return_value=["8.8.8.8"])), \
             patch.object(safe_browser, "ping_ms", AsyncMock(return_value=501)):
            with self.assertRaisesRegex(ValueError, "延迟"):
                await safe_browser.fetch_document("https://github.com/", ["github.com"])
        self.assertEqual(calls, [])

    async def test_unlisted_public_domain_is_allowed_when_restriction_is_off(self):
        calls = []
        with self.http([{}], calls), patch.object(safe_browser, "resolve", AsyncMock(return_value=["8.8.8.8"])), \
             patch.object(safe_browser, "ping_ms", AsyncMock(return_value=299)):
            result = await safe_browser.fetch_document("https://example.org/")
        self.assertEqual(result["text"], "safe")
        self.assertEqual(calls, ["https://example.org/"])

    async def test_exactly_500ms_is_rejected(self):
        calls = []
        with self.http([], calls), patch.object(safe_browser, "resolve", AsyncMock(return_value=["8.8.8.8"])), \
             patch.object(safe_browser, "ping_ms", AsyncMock(return_value=500)):
            with self.assertRaisesRegex(ValueError, "延迟"):
                await safe_browser.fetch_document("https://example.org/")
        self.assertEqual(calls, [])

    async def test_media_large_body_and_unsafe_text_are_rejected(self):
        for data in ({"mime": "image/png"}, {"body": b"x" * 500001}, {"body": "<p>色情</p>".encode()}, {"body": b"<script>go()</script>"}):
            with self.http([data], []), patch.object(safe_browser, "resolve", AsyncMock(return_value=["8.8.8.8"])), \
                 patch.object(safe_browser, "ping_ms", AsyncMock(return_value=20)):
                with self.assertRaises(ValueError):
                    await safe_browser.fetch_document("https://github.com/", ["github.com"])


if __name__ == "__main__": unittest.main()
