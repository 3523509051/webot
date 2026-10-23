"""Real Playwright form submission; search pages use checked, pinned HTTP I/O."""
import asyncio
import base64
import json
from urllib.parse import parse_qs, urlsplit

from safe_browser import ping_ms, resolve
from plugins.astrbot_plugin_group_activation.web_policy import (
    blocked_text, check_domain, clean_url, should_skip_ping)

ENGINES = {
    "bing": {"name": "必应网页版", "home": "https://cn.bing.com/", "domain": "bing.com",
             "input": 'input[name="q"]', "results": "#b_results > li.b_algo", "links": "h2 a", "snippet": ".b_caption p"},
    "baidu": {"name": "百度网页版", "home": "https://www.baidu.com/", "domain": "baidu.com",
              "input": 'input[name="wd"]', "results": "#content_left .result, #content_left .c-container",
              "links": "h3 a", "snippet": ".c-abstract, .content-right_8Zs40, .c-span-last"},
}


def result_url(raw):
    """Bing uses base64 target URLs; expose the destination, not a tracking link."""
    try:
        parsed = urlsplit(raw)
        if parsed.hostname in ("www.bing.com", "cn.bing.com") and parsed.path == "/ck/a":
            target = parse_qs(parsed.query).get("u", [""])[0]
            if target.startswith("a1"):
                target = target[2:]
                raw = base64.urlsafe_b64decode(target + "=" * (-len(target) % 4)).decode("utf-8")
        return clean_url(raw)
    except (ValueError, UnicodeError):
        return ""


async def checked_document(url, engine, request_headers, cache):
    """Return original HTML to the browser, preserving native form navigation."""
    import aiohttp
    import socket
    url = check_domain(url, [engine["domain"]])
    host = urlsplit(url).hostname
    if host not in cache:
        allow_fake_ip = bool(cache.get("allow_fake_ip", True))
        allow_private_ips = bool(cache.get("allow_private_ips", False))
        addresses = await resolve(host, allow_fake_ip, allow_private_ips)
        ip = next((item for item in addresses if ":" not in item), addresses[0])
        latency = None
        if not should_skip_ping(ip, allow_fake_ip, allow_private_ips):
            latency = await ping_ms(ip)
            if latency >= cache.get("max_ping_ms", 500):
                raise ValueError("搜索引擎 ping 延迟未低于配置上限")
        cache[host] = (ip, latency)
    ip, _ = cache[host]
    class Resolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, requested_host, port=443, family=0):
            if requested_host != host:
                raise ValueError("搜索请求域名发生变化")
            return [{"hostname": host, "host": ip, "port": port,
                     "family": socket.AF_INET6 if ":" in ip else socket.AF_INET, "proto": 0, "flags": 0}]
        async def close(self): pass
    # Only ephemeral search-session cookies; no personal profile/auth headers.
    headers = {key: value for key, value in request_headers.items()
               if key.lower() in ("user-agent", "accept", "accept-language", "cookie", "referer")}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=Resolver(), use_dns_cache=False),
                                     cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                                     timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get(url, headers=headers, allow_redirects=False) as response:
            if response.status in (301, 302, 303, 307, 308):
                return response.status, {"location": response.headers.get("Location", "")}, b""
            if response.status != 200:
                raise ValueError(f"搜索页面返回 HTTP {response.status}")
            if response.content_type not in ("text/html", "application/xhtml+xml"):
                raise ValueError("搜索页面不是 HTML")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(16384):
                size += len(chunk)
                if size > 2000000:
                    raise ValueError("搜索页面过大，已停止读取")
                chunks.append(chunk)
            response_headers = {"content-type": response.headers.get("Content-Type", "text/html")}
            if "Set-Cookie" in response.headers:
                response_headers["set-cookie"] = response.headers["Set-Cookie"]
            # Native GET forms work without site scripts, media or extra network.
            response_headers["content-security-policy"] = "default-src 'none'; form-action 'self'; base-uri 'self'"
            return 200, response_headers, b"".join(chunks)


async def submit_search(page, query, engine):
    await page.goto(engine["home"], wait_until="domcontentloaded", timeout=15000)
    field = page.locator(engine["input"]).first
    await field.fill(query, timeout=5000)
    await asyncio.gather(
        page.wait_for_url(lambda url: "/search" in url or "/s?" in url, wait_until="domcontentloaded", timeout=20000),
        field.press("Enter"))
    # Extraction only; this script is our own, website JavaScript stays disabled.
    rows = await page.locator(engine["results"]).evaluate_all("""(nodes, selectors) => nodes.slice(0, 12).map(node => {
        const link = node.querySelector(selectors.links);
        const summary = node.querySelector(selectors.snippet);
        return {title: link?.innerText || '', url: link?.href || '',
                snippet: (summary?.innerText || node.innerText || '').slice(0, 500)};
    })""", {"links": engine["links"], "snippet": engine["snippet"]})
    return rows


async def browser_search(query, settings, top_k=6):
    if not settings.get("enabled", True):
        return {"ok": False, "error": "后台浏览器访问已关闭"}
    query = str(query).strip()[:200]
    if not query or blocked_text(query):
        return {"ok": False, "error": "搜索词为空或未通过内容检查"}
    engine = ENGINES.get(settings.get("search_engine", "bing"))
    if engine is None:
        return {"ok": False, "error": "搜索引擎配置仅支持 bing 或 baidu"}
    async def run():
        from playwright.async_api import async_playwright
        cache = {"max_ping_ms": float(settings.get("max_ping_ms", 500)),
                 "allow_fake_ip": bool(settings.get("allow_fake_ip", True)),
                 "allow_private_ips": bool(settings.get("allow_private_ips", False))}
        errors, document_count = [], [0]
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel=settings.get("browser_channel", "msedge"), headless=True)
            try:
                context = await browser.new_context(java_script_enabled=False, accept_downloads=False,
                                                    service_workers="block", locale="zh-CN")
                async def route_request(route):
                    request = route.request
                    if request.resource_type != "document" or request.method != "GET":
                        await route.abort()
                        return
                    try:
                        document_count[0] += 1
                        if document_count[0] > 6:
                            raise ValueError("搜索页面跳转过多")
                        status, headers, body = await checked_document(request.url, engine, await request.all_headers(), cache)
                        await route.fulfill(status=status, headers=headers, body=body)
                    except ValueError as exc:
                        errors.append(str(exc))
                        await route.abort()
                    except Exception:
                        errors.append("搜索引擎连接失败或超时")
                        await route.abort()
                await context.route("**/*", route_request)
                page = await context.new_page()
                try:
                    rows = await submit_search(page, query, engine)
                except Exception:
                    if errors:
                        raise ValueError(errors[0])
                    raise
                if errors:
                    raise ValueError(errors[0])
                if not rows:
                    text = await page.inner_text("body")
                    if any(word in text.lower() for word in ("验证码", "验证", "captcha", "unusual traffic")):
                        raise ValueError("搜索引擎要求验证码，已停止；不会绕过验证")
                    raise ValueError("搜索页未返回可读取的结果，可能页面结构变化或不支持无脚本表单")
                results, seen = [], set()
                for row in rows:
                    url = result_url(row.get("url", ""))
                    if url and url not in seen and row.get("title"):
                        seen.add(url)
                        results.append({"title": str(row["title"])[:160], "url": url,
                                        "snippet": str(row.get("snippet", ""))[:300]})
                    if len(results) >= min(6, max(1, int(top_k))):
                        break
                if blocked_text(json.dumps(results, ensure_ascii=False)):
                    raise ValueError("搜索结果未通过内容检查，不返回原文")
                # cache 里除域名外还有 max_ping_ms / allow_fake_ip 两个配置键，
                # 取值时要按「是否为 (ip, latency) 元组」过滤。
                latencies = [pair[1] for pair in cache.values()
                             if isinstance(pair, tuple) and pair[1] is not None]
                return {"ok": True, "source": engine["name"], "results": results,
                        "ping_ms": max(latencies) if latencies else None}
            finally:
                await browser.close()
    try:
        return await asyncio.wait_for(run(), 40)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        return {"ok": False, "error": "后台浏览器搜索失败或超时，请检查网络和浏览器"}
