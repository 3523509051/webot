"""微信公众号文章专用只读抓取。

mp.weixin.qq.com 对无 JS 的自动化浏览器常返回“环境异常/验证”页，
但普通 HTTP + 桌面/手机 UA 往往能拿到完整 HTML。这里只处理
mp.weixin.qq.com，提取 #js_content 的正文，不执行脚本。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import aiohttp


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: list[str] = []
        self.in_content = False
        self.depth = 0
        self.content_parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class") or ""
        if tag == "h1" and "rich_media_title" in cls:
            self.in_title = True
        if attrs.get("id") == "js_content":
            self.in_content = True
            self.depth = 1
            return
        if self.in_content:
            self.depth += 1
            if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "section"):
                self.content_parts.append("\n")
            if tag in ("script", "style", "noscript", "svg", "iframe", "template"):
                self.hidden += 1

    def handle_endtag(self, tag):
        if self.in_title and tag == "h1":
            self.in_title = False
        if self.in_content:
            if tag in ("script", "style", "noscript", "svg", "iframe", "template") and self.hidden:
                self.hidden -= 1
            self.depth -= 1
            if self.depth <= 0:
                self.in_content = False

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if self.in_content and not self.hidden:
            self.content_parts.append(data)

    def title(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.title_parts)).strip()

    def content(self) -> str:
        text = "".join(self.content_parts)
        text = re.sub(r"[ \t\u00a0]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    enc = m.group(1) if m else "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def is_wechat_article(url: str) -> bool:
    try:
        u = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    return u.scheme in ("http", "https") and u.hostname in (
        "mp.weixin.qq.com",
        "mp.weixin.qq.com.",
    )


async def read_wechat_article(url: str, max_bytes: int = 6_000_000, max_chars: int = 16000):
    if not is_wechat_article(url):
        return {"ok": False, "error": "不是微信公众号文章链接"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://mp.weixin.qq.com/",
    }
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                final_url = str(resp.url)
                if resp.status != 200:
                    return {
                        "ok": False,
                        "error": f"HTTP {resp.status}",
                        "url": final_url,
                    }
                if resp.content_length and resp.content_length > max_bytes:
                    return {"ok": False, "error": "文章页面过大，拒绝读取"}
                chunks = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        return {"ok": False, "error": "文章页面过大，拒绝读取"}
                    chunks.append(chunk)
                body = b"".join(chunks)
                page = _decode(body, resp.headers.get("Content-Type", ""))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"公众号文章读取失败：{e}"}

    if "环境异常" in page or "wappoc_appmsgcaptcha" in final_url:
        return {
            "ok": False,
            "error": "微信返回环境异常/验证页，公众号正文被拦截",
            "url": final_url,
        }
    if "参数错误" in page and "js_content" not in page:
        return {
            "ok": False,
            "error": "微信返回参数错误（链接可能被截断或缺少文章参数）",
            "url": final_url,
        }

    parser = _ArticleParser()
    try:
        parser.feed(page)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"文章 HTML 解析失败：{e}"}
    title = parser.title()
    text = parser.content()
    if not text:
        return {
            "ok": False,
            "error": "页面里没有找到公众号正文（可能是视频号/登录/JS 页面）",
            "url": final_url,
            "title": title,
        }
    truncated = len(text) > max_chars
    return {
        "ok": True,
        "url": final_url,
        "title": title,
        "text": text[:max_chars],
        "truncated": truncated,
        "source": "wechat_article",
    }
