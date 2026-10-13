"""Read-only website reader: pinned HTTPS fetch, then isolated browser rendering.

Raw website scripts/media never enter the browser. Only escaped extracted text
is rendered. Dynamic pages requiring JavaScript are deliberately unsupported.
"""
import asyncio
from html import escape
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import socket
import sys
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins.astrbot_plugin_group_activation.web_policy import (
    DEFAULT_DOMAINS, blocked_text, check_domain, public_addresses)


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []
        self.starts = {"article": [], "main": []}
        self.sections = {"article": [], "main": []}
    def handle_starttag(self, tag, attrs):
        if not self.hidden and tag in self.starts:
            self.starts[tag].append(len(self.parts))
        if tag in ("script", "style", "noscript", "iframe", "svg", "template"):
            self.hidden.append(tag)
        if not self.hidden and tag in ("p", "div", "br", "li", "h1", "h2", "h3"):
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if not self.hidden and tag in self.starts and self.starts[tag]:
            self.sections[tag].append((self.starts[tag].pop(), len(self.parts)))
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
    def text(self):
        sections = self.sections["article"] or self.sections["main"]
        parts = [part for start, end in sections for part in self.parts[start:end]] if sections else self.parts
        return re.sub(r"[ \t]+", " ", "".join(parts)).strip()


async def ping_ms(ip):
    # Numeric IP only, exec arguments only; never pass user URLs to a shell.
    ping = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "PING.EXE")
    args = [ping, "-n", "2", "-w", "1000", ip] if sys.platform == "win32" else ["ping", "-n", "-c", "2", "-W", "1", ip]
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.DEVNULL,
                                                  **({"creationflags": 0x08000000} if sys.platform == "win32" else {}))
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 4)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    raw = stdout.decode("ascii", "ignore")
    # Reply lines contain TTL; ignore localized summary lines and failed replies.
    values = re.findall(r"[=<]\s*(\d+(?:\.\d+)?)\s*ms[^\r\n]*TTL", raw, re.I)
    if not values:  # Linux ping format
        values = re.findall(r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", raw, re.I)
    if process.returncode != 0 or not values:
        raise ValueError("网站 ping 不通，按当前规则拒绝访问")
    return sum(float(x) for x in values) / len(values)


async def resolve(host):
    records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(record[4][0] for record in records))
    return public_addresses(ips)


async def fetch_document(url, domains=None, max_ping_ms=300, max_bytes=500000):
    import aiohttp
    current = url
    for _ in range(4):
        current = check_domain(current, domains)
        if blocked_text(current):
            raise ValueError("网址含不适合群聊的内容关键词，已阻止访问")
        host = urlsplit(current).hostname
        addresses = await resolve(host)
        ip = next((x for x in addresses if ":" not in x), addresses[0])
        latency = await ping_ms(ip)
        if latency >= max_ping_ms:
            raise ValueError("网站 ping 延迟未低于配置上限")
        # Pin the validated address, preserving host/SNI/TLS certificate checks.
        class PinnedResolver(aiohttp.abc.AbstractResolver):
            async def resolve(self, requested_host, port=443, family=0):
                if requested_host != host:
                    raise ValueError("请求域名发生变化")
                return [{"hostname": host, "host": ip, "port": port,
                         "family": socket.AF_INET6 if ":" in ip else socket.AF_INET,
                         "proto": 0, "flags": 0}]
            async def close(self): pass
        connector = aiohttp.TCPConnector(resolver=PinnedResolver(), use_dns_cache=False)
        async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                         cookie_jar=aiohttp.DummyCookieJar(),
                                         timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(current, allow_redirects=False,
                                   headers={"User-Agent": "WebotReadOnly/0.14"}) as response:
                if response.status in (301, 302, 303, 307, 308):
                    target = response.headers.get("Location")
                    if not target:
                        raise ValueError("跳转缺少目标网址")
                    current = urljoin(current, target)
                    continue
                if response.status != 200:
                    raise ValueError(f"网站返回 HTTP {response.status}")
                if response.headers.get("Content-Disposition", "").lower().startswith("attachment"):
                    raise ValueError("不允许下载附件")
                mime = response.content_type
                if mime not in ("text/html", "text/plain", "application/xhtml+xml"):
                    raise ValueError("只读取网页文字，不读取图片、视频或文件")
                if response.content_length and response.content_length > max_bytes:
                    raise ValueError("网页过大，拒绝读取")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(16384):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("网页过大，拒绝读取")
                    chunks.append(chunk)
                document = b"".join(chunks).decode(response.charset or "utf-8", "replace")
                if mime == "text/plain":
                    text = document.strip()
                else:
                    parser = TextParser()
                    parser.feed(document)
                    text = parser.text()
                if blocked_text(text) or blocked_text(current):
                    raise ValueError("网页含不适合群聊的内容，已阻止返回")
                if not text:
                    raise ValueError("网页没有可读取文字，可能需要 JavaScript、登录或验证")
                return {"url": current, "ping_ms": round(latency, 1), "text": text}
    raise ValueError("网站重定向次数过多")


async def read_website(url, settings):
    if not settings.get("enabled", True):
        return {"ok": False, "error": "网站访问已关闭"}
    async def read():
        domains = settings.get("allowed_domains", DEFAULT_DOMAINS) if settings.get("restrict_domains", False) else None
        result = await fetch_document(url, domains,
                                      float(settings.get("max_ping_ms", 300)))
        # Browser does not perform any website network request or execute scripts.
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                channel=settings.get("browser_channel", "msedge"), headless=True)
            try:
                context = await browser.new_context(java_script_enabled=False,
                                                    accept_downloads=False, service_workers="block")
                await context.route("**/*", lambda route: route.abort())
                page = await context.new_page()
                await page.set_content("<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'\"><pre>"
                                       + escape(result["text"]) + "</pre>")
                result["text"] = await page.inner_text("body")
            finally:
                await browser.close()
        result["text"] = result["text"][:8000]
        return {"ok": True, **result}
    try:
        return await asyncio.wait_for(read(), 25)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except ImportError:
        return {"ok": False, "error": "桥接缺少 aiohttp/playwright，请按部署文档安装"}
    except Exception:
        return {"ok": False, "error": "网站读取失败或超时；请检查浏览器安装和网络"}
