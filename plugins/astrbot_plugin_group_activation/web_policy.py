"""Shared policy; no AstrBot imports so the Windows bridge can use it."""
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

DEFAULT_DOMAINS = ["baidu.com", "amap.com", "gov.cn", "people.com.cn",
                   "xinhuanet.com", "ctrip.com", "mafengwo.cn", "12306.cn",
                   "github.com", "wikipedia.org", "wikimedia.org"]
BLOCKED = re.compile(r"色情|成人视频|裸聊|血腥|肢解|斩首|虐杀|porn(?:ography)?|gore|dismember|beheading", re.I)


def clean_url(url):
    if not isinstance(url, str) or len(url) > 2000 or re.search(r"[\s\\\x00-\x1f]", url):
        raise ValueError("网址格式不受支持")
    try:
        if "://" not in url:
            url = "http://" + url
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        standard_port = {"http": 80, "https": 443}.get(parsed.scheme)
        if standard_port is None or parsed.port not in (None, standard_port) or parsed.username or parsed.password:
            raise ValueError("仅允许 HTTP/HTTPS 标准端口，不允许网址携带账号密码")
        if not host or not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host:
            raise ValueError("无效域名")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("不允许直接访问 IP 地址")
        return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))
    except (UnicodeError, TypeError) as exc:
        raise ValueError("无效网址") from exc


def check_domain(url, domains=None):
    url = clean_url(url)
    if domains is None:
        return url
    host = urlsplit(url).hostname
    if not any(host == domain.lower() or host.endswith("." + domain.lower())
               for domain in domains if isinstance(domain, str) and "." in domain and "*" not in domain):
        raise ValueError("域名不在网站访问白名单")
    return url


def public_addresses(addresses):
    ips = [ipaddress.ip_address(ip) for ip in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
        raise ValueError("网站解析到本机、内网或保留地址，禁止访问")
    return addresses


def revokes_website(text):
    text = re.sub(r'「[^」]*」|“[^”]*”|"[^"]*"', "", text or "")
    return bool(re.search(r"(?:不要|不用|别|禁止|取消).{0,6}(?:访问|打开|浏览|爬|抓|查看)", text))


def authorized_urls(text):
    text = re.sub(r'「[^」]*」|“[^”]*”|"[^"]*"', "", text or "")
    if revokes_website(text):
        return []
    urls = []
    candidates = list(re.findall(r"https?://[^\s<>\"'，。！？；（）【】]+", text, re.I))
    remaining = re.sub(r"https?://[^\s<>\"'，。！？；（）【】]+", " ", text, flags=re.I)
    candidates += re.findall(r"(?<![\w@/:-])(?:[\w\-]+\.)+[a-zA-Z]{2,63}(?:/[^\s<>\"'，。！？；（）【】]*)?", remaining)
    for raw in candidates:
        if "://" not in raw:
            raw = re.sub(r"^(?:请|帮我|麻烦你|你)?(?:访问|打开|看看|查看|阅读)", "", raw)
        # WeChat links are often pasted immediately before a Chinese question.
        suffix = re.search(r"(?:这(?:个|篇|条|段)|该)(?:视频|网页|页面|文章|链接)", raw)
        candidate = raw[:suffix.start()] if suffix else raw
        try:
            url = clean_url(candidate.rstrip(".,!?;)]}"))
        except ValueError:
            continue
        if url not in urls:
            urls.append(url)
    # The caller requires a real @; any URL in that message now grants reading.
    return urls[:3]


def blocked_text(text):
    return bool(BLOCKED.search(text or ""))
