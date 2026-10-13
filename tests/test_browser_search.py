"""Offline browser-search controls and network-policy regression tests."""
import asyncio
import base64
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import browser_search as search
sys.path.pop(0)


class BrowserSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_form_operations_are_used(self):
        navigation = asyncio.Event()
        field = types.SimpleNamespace(fill=AsyncMock(), press=AsyncMock(side_effect=lambda key: navigation.set()))
        results = types.SimpleNamespace(evaluate_all=AsyncMock(return_value=[{"title": "学校", "url": "https://example.org/"}]))
        def locator(selector):
            return types.SimpleNamespace(first=field) if selector == 'input[name="q"]' else results
        async def wait_for_url(predicate, **kwargs):
            await navigation.wait()
            self.assertTrue(predicate("https://cn.bing.com/search?q=school"))
        page = types.SimpleNamespace(goto=AsyncMock(), locator=locator, wait_for_url=wait_for_url)
        rows = await search.submit_search(page, "学校官网", search.ENGINES["bing"])
        page.goto.assert_awaited_once()
        field.fill.assert_awaited_once_with("学校官网", timeout=5000)
        field.press.assert_awaited_once_with("Enter")
        self.assertEqual(rows[0]["title"], "学校")

    async def test_tracking_url_exposes_destination_and_unsafe_scheme_is_rejected(self):
        target = "https://www.sztu.edu.cn/"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        self.assertEqual(search.result_url("https://www.bing.com/ck/a?u=a1" + encoded), target)
        self.assertEqual(search.result_url("javascript:alert(1)"), "")
        self.assertEqual(search.result_url("https://127.0.0.1/"), "")

    async def test_exactly_300ms_search_engine_is_rejected(self):
        with patch.object(search, "resolve", AsyncMock(return_value=["8.8.8.8"])), \
             patch.object(search, "ping_ms", AsyncMock(return_value=300)):
            with self.assertRaisesRegex(ValueError, "延迟"):
                await search.checked_document("https://cn.bing.com/", search.ENGINES["bing"], {}, {"max_ping_ms": 300})

    async def test_search_engine_redirect_cannot_open_arbitrary_site(self):
        with patch.object(search, "resolve", AsyncMock()) as resolve:
            with self.assertRaisesRegex(ValueError, "白名单"):
                await search.checked_document("http://evil.example/", search.ENGINES["bing"], {}, {})
        resolve.assert_not_awaited()

    def fake_playwright(self, body="", rows=None):
        owner = self
        page = types.SimpleNamespace(inner_text=AsyncMock(return_value=body))
        async def route(pattern, handler): owner.handler = handler
        context = types.SimpleNamespace(route=route, new_page=AsyncMock(return_value=page))
        self.browser = types.SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        playwright = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=AsyncMock(return_value=self.browser)))
        class Manager:
            async def __aenter__(self): return playwright
            async def __aexit__(self, *args): pass
        return patch.dict(sys.modules, {"playwright.async_api": types.SimpleNamespace(async_playwright=Manager)})

    async def test_captcha_is_reported_and_browser_is_closed(self):
        with self.fake_playwright(body="请完成验证码"), patch.object(search, "submit_search", AsyncMock(return_value=[])):
            result = await search.browser_search("学校官网", {})
        self.assertFalse(result["ok"])
        self.assertIn("验证码", result["error"])
        self.browser.close.assert_awaited_once()

    async def test_search_returns_results_and_blocks_media_requests(self):
        async def document(url, engine, headers, cache):
            cache["cn.bing.com"] = ("8.8.8.8", 20)
            return 200, {"content-type": "text/html"}, b"<html></html>"
        async def submit(page, query, engine):
            media = types.SimpleNamespace(request=types.SimpleNamespace(resource_type="image", method="GET"), abort=AsyncMock())
            await self.handler(media)
            media.abort.assert_awaited_once()
            request = types.SimpleNamespace(resource_type="document", method="GET", url="https://cn.bing.com/", all_headers=AsyncMock(return_value={}))
            route = types.SimpleNamespace(request=request, fulfill=AsyncMock(), abort=AsyncMock())
            await self.handler(route)
            route.fulfill.assert_awaited_once()
            return [{"title": "学校", "url": "https://www.sztu.edu.cn/", "snippet": "官网"}]
        with self.fake_playwright(), patch.object(search, "submit_search", side_effect=submit), \
             patch.object(search, "checked_document", side_effect=document):
            result = await search.browser_search("学校官网", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["results"][0]["url"], "https://www.sztu.edu.cn/")
        self.assertEqual(result["ping_ms"], 20)
        self.assertFalse(self.browser.new_context.await_args.kwargs["java_script_enabled"])
        self.assertFalse(self.browser.new_context.await_args.kwargs["accept_downloads"])
        self.browser.close.assert_awaited_once()

    async def test_bad_engine_and_unsafe_query_do_not_start_browser(self):
        with self.fake_playwright():
            for query, settings in (("学校", {"search_engine": "invalid"}), ("色情", {})):
                self.assertFalse((await search.browser_search(query, settings))["ok"])
        self.browser.new_context.assert_not_awaited()


if __name__ == "__main__": unittest.main()
