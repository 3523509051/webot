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


# 代理软件的 fake-ip 段（Clash / mihomo / sing-box 默认 198.18.0.0/15）。
# 开了 TUN + fake-ip 的机器上，域名会被解析成这里的「假地址」，真正的转发由 TUN 栈
# 接管 —— 它不是内网地址。若一律当「保留地址」拒绝，网站读取会**随机**失败：
# 2026-10-14 实测同一网址 13:17 解析成 198.18.0.93 被拒，13:18 解析成真实 IP 就成功。
PROXY_FAKE_NETS = (ipaddress.ip_network("198.18.0.0/15"),)


def is_proxy_fake_ip(ip):
    """该地址是否属于代理软件的 fake-ip 段。"""
    return any(ip in net for net in PROXY_FAKE_NETS)


def public_addresses(addresses, allow_fake_ip=False, allow_private_ips=False):
    """校验域名解析结果，决定能不能去连。

    allow_fake_ip     : 放行代理软件的 fake-ip 段（198.18.0.0/15）。它不是内网地址 ——
                        开了 TUN + fake-ip 的机器上几乎所有域名都解析到这里，
                        一律当「保留地址」拒绝会导致网站读取随机失败（2026-10-14 实测）。
    allow_private_ips : **完全放开**：本机、内网、保留地址一律允许。默认关闭；
                        打开后 SSRF 防护失效（可用于访问内网服务），只应在完全
                        信任使用者的本机部署里打开。
    """
    ips = [ipaddress.ip_address(ip) for ip in addresses]
    if not ips:
        raise ValueError("网站解析到本机、内网或保留地址，禁止访问")
    if allow_private_ips:
        return addresses
    for ip in ips:
        if allow_fake_ip and is_proxy_fake_ip(ip):
            continue          # 代理 fake-ip：由本机代理转发到真实站点
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ValueError("网站解析到本机、内网或保留地址，禁止访问")
    return addresses


def should_skip_ping(ip, allow_fake_ip=False, allow_private_ips=False):
    """本机/内网/代理 fake-ip 地址不做 ping 延迟门槛 —— ping 对它们没有意义，
    还可能直接不通（内网服务常关 ICMP）。不跳过的话，「放开地址限制」会被延迟
    检查二次卡死：地址允许了、ping 却过不去。公网地址仍按配置的门槛检查。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if allow_fake_ip and is_proxy_fake_ip(addr):
        return True
    if allow_private_ips and (not addr.is_global or addr.is_multicast or addr.is_reserved):
        return True
    return False


# 用户/模型常把动词并进网址里（`搜索深圳奶蛙大学.com`）。
# 2026-10-14 实测：模型把「搜索」也拼进域名，转码成 xn--fes68b0gz4eq3rrlo17ythr.com
# （反解就是「搜索深圳奶蛙大学.com」）→ 该域名不存在，读取 0.4 秒即失败。
LEADING_VERB = re.compile(
    r"^(?:请|帮我|麻烦你|你)?(?:搜索一下|搜一下|搜索|搜|查询一下|查询|查一下|查|"
    r"访问|打开|看看|看一下|查看|阅读|读一下|读)+"
)


def strip_leading_verb(text):
    """去掉网址前面被顺手带上的动词：`搜索xxx.com` -> `xxx.com`。"""
    return LEADING_VERB.sub("", (text or "").strip()).strip()


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
            raw = strip_leading_verb(raw)
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
