# -*- coding: utf-8 -*-
"""
微信 -> AstrBot 桥（Windows）

链路：
    微信客户端
      ↑ 读: WeChatDB(本地数据库增量)   ↓ 写: WeChatGUI(UIA/OCR)
    本程序
      ↕ OneBot v11 反向 WebSocket
    AstrBot (aiocqhttp, 默认 127.0.0.1:6199)

用法：
    .\\.venv\\Scripts\\python.exe main.py
"""
from __future__ import annotations

import asyncio
import base64
import collections
import html
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path

from onebot11 import OneBotClient, ActionFailed
from send_result import SendResult
from wechat_client import (WeChatClient, cached_image, cached_image_by_time,
                           cached_sticker, _zstd_text)
from safe_browser import read_website
from browser_search import browser_search
from wechat_article import read_wechat_article, is_wechat_article
from sticker_library import StickerLibrary
from file_library import FileLibrary
from identity_directory import IdentityDirectory
from private_chat_handler import (
    PrivateAttempts,
    SyncResult,
    sync_authorized_privates,
    redeem_activation_code,
    is_activation_code_format,
    private_user_id,
)
from unified_poll import (
    KIND_GROUP,
    KIND_PRIVATE,
    UnifiedPoller,
    default_event_filter,
)
from private_gate import PrivateGate, collect_startup_seq

CONFIG_PATH = Path(__file__).with_name("config.json")

# 模块级 logger：build_segments / image_segment / card_segments 这些都是**模块级函数**，
# 也要打日志。早先只在 main() 里定义了 log，结果这些函数一走到日志分支就
# NameError 崩掉（用真实引用回复消息做本地验证时才暴露出来）。
log = logging.getLogger("bridge")

# 表情库是本地数据，不进 Git；路径固定在仓库根目录的 stickers/。
STICKERS = StickerLibrary(Path(__file__).resolve().parent.parent / "stickers")
FILE_LIBRARY = FileLibrary(Path(__file__).resolve().parent.parent / "files")


def _pero_media_mime(data: bytes):
    """P12：按字节签名判断 Pero 媒体动作的真实图片类型；未知一律拒绝。"""
    if not isinstance(data, bytes):
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return None


# P12-R01：按 (账号, 目标, 业务 action_id) 的有界执行防重表；
# 重复同一业务动作不再次 GUI 操作，也不覆盖在途任务。
_PERO_MEDIA_DEDUP_MAX = 128
_PERO_MEDIA_DEDUP: "collections.OrderedDict[tuple, float]" = collections.OrderedDict()

DEFAULT_CONFIG = {
    "astrbot": {
        "ws_url": "ws://127.0.0.1:6199/ws",
        "token": "",
        "self_id": 10001,
        "heartbeat_seconds": 15,
    },
    "wechat": {
        # bot_nickname / bot_nicknames 只用于「补充」@ 识别别名。
        # 主要来源是从微信数据库实时读到的自己昵称（改名后无需改这里）。
        "bot_nickname": "群助手",
        "bot_nicknames": [],
        "poll_interval": 1.5,
        "group_whitelist": [],
        "groups": {},
        "self_wxid": "",
        "db_dir": "",
        # 发送后回读数据库确认（更可靠但最多多等 8 秒）——追求响应速度时关闭
        "verify_send": False,
        # 拟人节奏层档位：natural / calm / fast / off
        "rhythm": "fast",
        # 图片通道：把群友发的图片解密后，以 base64 上报给 AstrBot
        # （provider 声明了 image 模态，模型可直接看图，路径见 docs/architecture.md）。
        # 为什么用 base64 而不是 file:// —— AstrBot 跑在容器里，读不到宿主机路径。
        # 超过 image_max_bytes 的图片退化成 [图片] 占位文本，避免 WS 大帧传输失败。
        "send_images": True,
        "image_max_bytes": 10_000_000,
        "image_max_edge": 0,  # 0 = 不缩分辨率；长边像素上限（非字节）
        # 动画表情（表情包）当图片处理：从 XML 的 cdnurl 取原文件，动图只取第一帧
        "fetch_stickers": True,
        # 收到的图片按 md5 存一份到 bridge\cache\image\，供**以后引用旧图**时找回
        # （表情包本来就有 md5 缓存；图片不存的话，微信清理本地副本后引用就找不到）。
        # 0 = 关闭；-1 = 保留所有（索引/文件都不自动裁剪）
        "image_cache_keep": -1,
        # 先发图、再问「这是什么」时，把上一条图补挂到追问上的有效期（秒）。
        # 0 = 关闭。AstrBot 侧对纯图片事件不一定触发插件缓存，桥这里兜底。
        "image_carry_seconds": 30.0,
        # 一条回复最多转换几个真实 @（微信 @ 令牌要逐个用 UIA 点选）。
        "max_mentions": 3,
        # 统一增量读取调度（群聊与已授权私聊同级轮转）：
        # 每会话每轮最多读几条增量事件、分发队列上限、指标汇总日志间隔。
        "read_batch": 50,
        "dispatch_queue_max": 200,
        "metrics_log_seconds": 30.0,
        # 入站上报失败重试：传输类失败（未连接/写帧异常）无限重试直到恢复；
        # 其它错误（媒体解密等）最多尝试 N 次，之后明确放弃并计数。
        "dispatch_max_attempts": 3,
        "dispatch_retry_base": 0.5,
        "dispatch_retry_max": 10.0,
    },
    "private_chat": {
        "pero_url": "http://127.0.0.1:8765",
        "token_env": "PERO_MANAGEMENT_TOKEN",
        # 授权名单同步间隔（秒）；只更新名单，不参与常规聊天读取。
        "sync_seconds": 5.0,
        # 未授权会话发现 + 开通码监听间隔（秒）；0 = 关闭发现流程。
        "discover_seconds": 15.0,
    },
    # wechatauto_level=DEBUG 时会打出侧栏查找 / 兜底搜索 / open_chat 的细节，
    # 用于验证「已在目标群时不做切换」。零运行时开销（只是放行已有日志）。
    "logging": {"level": "INFO", "wechatauto_level": "DEBUG"},
    "website": {"enabled": True, "restrict_domains": False, "max_ping_ms": 500,
                "allow_fake_ip": True, "allow_private_ips": False,
                "browser_channel": "msedge", "search_engine": "bing"},
}


# 微信 @ 之后会跟各种不可见空白（thin space / nbsp / 全角空格 等）
MENTION_SPACES = r"[\s\u2005\u00a0\u3000\u2002\u2003\u2009\u202f]*"

# 「先发图、再问图」的兜底：后一条纯文字里没有图片段，桥把上一条图补挂上去。
# 只匹配确实在问图的说法，避免把「图书馆」之类误判成看图请求。
IMAGE_QUERY = re.compile(
    r"图|照片|截图|表情包|画面|看一下|看看|认一认|识别|读字|文字|上面写|这是什么"
)


def build_segments(text: str, bot_nicknames: list[str], self_id: int) -> list[dict]:
    """把微信的纯文本 @昵称 转成 OneBot v11 的 at 消息段。

    微信数据库里的 @ 是**纯文本、没有结构化字段**（实测 content 就是
    `wxid_xxx:\\n@昵称\\u2005`），所以只能靠昵称匹配 —— 别名必须配全。

    因此别名的第一来源是**数据库里实时读到的自己昵称**（见 main() 里组装
    aliases 的那几行）：在微信里改了昵称，这里不用跟着改配置。

    注意：**@ 可能出现在任意位置**（句首、句中、句尾），必须全局匹配，
    否则「我们刚刚聊了什么@大肥鱼」这种句尾 @ 会被漏掉。
    """
    text = (text or "").strip()
    if not text:
        return []

    # 长别名优先：避免短别名抢先匹配掉长别名
    names = sorted(
        {n.strip() for n in (bot_nicknames or []) if n and n.strip()},
        key=len,
        reverse=True,
    )
    if not names:
        return [{"type": "text", "data": {"text": text}}]

    pat = re.compile("(?:" + "|".join("@" + re.escape(n) for n in names) + ")" + MENTION_SPACES)
    segs: list[dict] = []
    pos = 0
    hit = False

    for m in pat.finditer(text):
        before = text[pos:m.start()].strip()
        if before:
            segs.append({"type": "text", "data": {"text": before}})
        segs.append({"type": "at", "data": {"qq": str(self_id)}})
        pos = m.end()
        hit = True

    if not hit:
        return [{"type": "text", "data": {"text": text}}]

    tail = text[pos:].strip()
    if tail:
        segs.append({"type": "text", "data": {"text": tail}})
    return segs


def find_mention(text: str, names: set[str]) -> tuple[str | None, str, str]:
    """从**出站**正文里找出模型写的 `@昵称`，返回 (成员名, 前半段, 后半段)。

    为什么在桥这层做，而不是让 AstrBot 直接发 at 段：
        AstrBot 的 `At.toDict()` **只序列化 `qq`、不带 name**；而 OneBot 的 qq 到
        我们这儿是个 crc32 假 ID，换不出微信 @ 面板要的「界面显示名」。
        所以走「模型写 @昵称 → 桥校验并转换」这条路，微信特有的知识全留在桥里。

    **为什么返回前后两段而不是把 @ 删掉**：微信的 @ 令牌只能"边打边选"地
    插入到光标处。如果只带回去掉 @ 的正文、再把 @ 补到结尾，模型写在句首的
    `@小泽布尔` 就会被挪到消息末尾（实测用户看到的「乱 @」正是这个）——
    必须按模型写的位置插，前面粘完再插 @ 令牌、最后接上后半段。

    只认**本群真实成员**：模型可能凭空写 @某某，那种一律按普通文本发出去 ——
    不冒险驱动界面去选一个不存在的人（白付一次 OCR + 点击）。
    一次只 @ 一个人，库的 at_member 就是单成员。
    """
    if not text or not names or "@" not in text:
        return None, text, ""
    ordered = sorted((n for n in names if n), key=len, reverse=True)
    if not ordered:
        return None, text, ""
    pat = re.compile("@" + "(?:" + "|".join(re.escape(n) for n in ordered) + ")")
    m = pat.search(text)
    if not m:
        return None, text, ""
    name = m.group(0)[1:]
    before = text[: m.start()].rstrip()
    after = text[m.end() :].lstrip()
    return name, before, after


def parse_mentions(
    text: str, names: set[str], max_count: int = 3
) -> list[dict]:
    """把正文里的 `@本群成员名` 解析成「文本 / @」交替的段。

    与 find_mention 的区别：这里保留**所有** @（最多 max_count 个），
    供桥逐个插入真实 @。返回例如：
        [{"type":"text","text":"前半"},
         {"type":"at","name":"小明"},
         {"type":"text","text":"后半"}]
    只认本群真实成员；同名只取第一次，重复的 @ 留作普通文字。
    """
    if not text or not names or "@" not in text:
        return []
    max_count = max(1, int(max_count or 1))
    ordered = sorted((n for n in names if n), key=len, reverse=True)
    if not ordered:
        return []
    pat = re.compile("@" + "(?:" + "|".join(re.escape(n) for n in ordered) + ")")
    selected = []
    seen: set[str] = set()
    for m in pat.finditer(text):
        name = m.group(0)[1:]
        if name in seen:
            continue
        if len(selected) >= max_count:
            break
        seen.add(name)
        selected.append(m)
    if not selected:
        return []

    parts: list[dict] = []
    pos = 0
    for m in selected:
        if m.start() > pos:
            parts.append({"type": "text", "text": text[pos : m.start()]})
        parts.append({"type": "at", "name": m.group(0)[1:]})
        pos = m.end()
    if pos < len(text):
        parts.append({"type": "text", "text": text[pos:]})

    # 去掉 @ 两侧多余空白（与 find_mention 的 rstrip/lstrip 保持一致）
    for i, part in enumerate(parts):
        if part["type"] != "text":
            continue
        if i > 0 and parts[i - 1]["type"] == "at":
            part["text"] = part["text"].lstrip()
        if i + 1 < len(parts) and parts[i + 1]["type"] == "at":
            part["text"] = part["text"].rstrip()
    return [p for p in parts if p["type"] != "text" or p["text"]]


# 拿不到原始文件时的占位文本：至少让模型知道「这里有什么」。
# ⚠️ 动画表情（表情包）**已经不在这里**：2026-10-09 起它按图片处理
#    （从消息 XML 的 cdnurl 取原文件，动图只取第一帧）——
#    见 wechat_client.WeChatClient.sticker_frame 与 docs/architecture.md §3.2。
#    这里只剩真正拿不到文件的类型（语音/视频）。
MEDIA_PLACEHOLDER = {
    "语音": "[语音]",
    "视频": "[视频]",
}

# build_media_segments 是**模块级函数**，拿不到 main() 里的局部 cfg ——
# 把这里要用的开关单独放一份，由 main() 启动时填（见 init_media_opts）。
# ⚠️ 教训（2026-10-09 实测踩到）：之前这里直接写 cfg["wechat"][...]，
# 结果每收到一个表情包都抛 `name 'cfg' is not defined`，整条消息被外层
# except 丢掉 → 群里表现就是「它不认识表情包」。
MEDIA_OPTS: dict = {"fetch_stickers": True, "image_max_edge": 0,
                    "image_cache_keep": -1}


def init_media_opts(cfg: dict) -> None:
    """把 main() 读到的配置同步给模块级媒体开关。"""
    wx_cfg = cfg.get("wechat") or {}
    MEDIA_OPTS["fetch_stickers"] = bool(wx_cfg.get("fetch_stickers", True))
    MEDIA_OPTS["image_max_edge"] = int(wx_cfg.get("image_max_edge", 0))
    MEDIA_OPTS["image_cache_keep"] = int(wx_cfg.get("image_cache_keep", -1))

# appmsg 的 <type> → 人类可读前缀（type 49「文件/链接/卡片」内部还分很多种）
APPMSG_LABELS = {
    "4": "链接",
    "5": "链接",
    "6": "文件",
    "8": "表情",
    "19": "聊天记录",
    "33": "小程序",
    "36": "小程序",
    "44": "小程序",
    "57": "引用回复",
    "87": "公告",
    "2000": "转账",
    "2001": "红包",
}


def _xml_tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.S)
    return (m.group(1) or "").strip() if m else ""


# 引用**表情包**时 refermsg 的 content：`wxid_xxx:<毫秒时间戳>:<序号>:<md5>::0`
_REFER_ID_RE = re.compile(r"^[^:\s<>]{4,}:(\d{10,16}):\d+:([0-9a-fA-F]{32})::\d+$")


def _pero_quote_ext(raw: str) -> dict | None:
    """P13：从 type 49 的 refermsg 抽取**结构化引用事实**（版本化）。

    只取微信消息元数据里的字段（displayname/chatusr/md5/createtime），
    不从正文"@昵称"猜身份；取不到的字段留空，不伪造。没有引用返回 None。
    """
    info = parse_appmsg(raw or "")
    if not info:
        return None
    refer_author = str(info.get("refer_author") or "").strip()
    refer_md5 = str(info.get("refer_md5") or "").strip()
    refer_time = str(info.get("refer_time") or "").strip()
    refer_content = str(info.get("refer_content") or "").strip()
    refer_chatusr = str(info.get("refer_chatusr") or "").strip()
    if not any((refer_author, refer_md5, refer_time, refer_content)):
        return None
    quote = {"v": 1, "source": "refermsg_xml"}
    if refer_author:
        quote["author_display"] = refer_author[:60]
    if refer_chatusr:
        quote["author_wxid"] = refer_chatusr[:64]
    if refer_md5:
        quote["image_md5"] = refer_md5
    if refer_time:
        quote["refer_time"] = refer_time[:20]
    if refer_content:
        quote["refer_text"] = refer_content[:200]
    return quote


def _fake_id(group_id: str) -> int:
    """把微信 ID 稳定映射成 OneBot 数字 ID，供消息与身份扩展共同使用。"""
    import zlib
    return zlib.crc32(group_id.encode("utf-8")) % (10 ** 9)


def _pero_identity_ext(m: dict, g: dict) -> dict:
    """P13：版本化身份扩展（v1）。只取消息元数据/配置里可得的字段。

    - `group_wxid` / `sender_wxid`：真实微信 ID（sender_wxid 来自消息 DB 的
      `wxid:` 前缀解析；无前缀消息为空，如实省略——不猜、不伪造）；
    - `bridge_group_id` / `bridge_user_id`：桥内稳定映射 ID（crc32），
      与 Pero 现有 raw_target_ref 桥 ID 语义一致；
    - `sender_name`：DB 昵称（查不到为 "unknown"）；`group_card` 桥暂无
      （OneBot card 恒空），字段省略；
    - `session`/`local_id`：桥会话与微信本地消息号，用于限定来源范围；
    - `sources`：每个字段来源说明，Pero 据此决定信任级别。
    """
    ext = {
        "v": 1,
        "group_wxid": str(g.get("id") or "")[:64],
        "bridge_group_id": _fake_id(g.get("id")),
        "bridge_user_id": _fake_id(m.get("sender_wxid") or m.get("sender_name")),
        "sender_name": str(m.get("sender_name") or "")[:60],
        "session": "",
        "sources": {
            "group_wxid": "bridge_group_config",
            "sender_wxid": "message_db_prefix",
            "sender_name": "message_db_nick",
        },
    }
    swxid = str(m.get("sender_wxid") or "").strip()
    if swxid:
        ext["sender_wxid"] = swxid[:64]
    local_id = str(m.get("local_id") or "").strip()
    if local_id:
        ext["local_id"] = local_id[:64]
    return ext


# P13-R01：Pero 专用**只读查询**（文件字节 / 引用原图）返回字节的硬上限。
# 整块协议只用于旧 Pero 回退，默认 8 MiB；新 Pero 走 `pero_file_chunk_v1`
# 分块（单块默认 1 MiB，可在 config.json 的 pero_file.max_chunk_bytes 调整）。
_PERO_QUERY_MAX_BYTES = 8 * 1024 * 1024


def _pero_resolve_quote_image(wx, group_wxid: str, md5: str, refer_time: str):
    """P13-R01：按引用事实（md5 / createtime）找回**当前群内**被引用图片的本地副本。

    查找链与 build_media_segments 的引用附图一致（都限定 group_wxid，不跨群）：
    ① 微信图片库 local_id 解密（md5 是 CDN 大图 md5，与本地常不同，
       createtime 才是可靠检索键）→ ② 表情静态帧缓存（md5）→
       ③ 图片缓存（md5）→ ④ 图片缓存（时间）。
    返回 (path, source)；找不到返回 (None, "")——**不拿其他图顶替**。
    """
    try:
        lid = wx.image_local_id_by_ref(group_wxid, md5, refer_time or None)
        if lid:
            path = wx.image_path(group_wxid, lid)
            if path:
                return str(path), f"wechat_image_db(local_id={lid})"
    except Exception:
        pass
    cached = cached_sticker(md5)
    if cached:
        return str(cached), f"emoji_cache({cached.name})"
    cached = cached_image(md5, group_wxid)
    if cached:
        return str(cached), f"image_cache(md5 {cached.name})"
    if refer_time:
        cached = cached_image_by_time(refer_time, group_wxid=group_wxid)
        if cached:
            return str(cached), f"image_cache(time {cached.name})"
    return None, ""


def parse_appmsg(text: str) -> dict | None:
    """解析 type 49（文件/链接/卡片）里的 appmsg XML。

    为什么必须解析：**引用回复也是 type 49**，而它的正文放在 `<title>` 里。
    整条当卡片丢掉的话，「@机器人 + 引用一张图问这是什么」会变成：
    @ 丢了 → 窗口不开 → 机器人装死（实测踩到过）。
    """
    if "<appmsg" not in text:
        return None
    # 只在被引用消息块里找，避免和外层同名标签混淆
    refer = _xml_tag(text, "refermsg")
    refer_body = _xml_tag(refer, "content") if refer else ""
    refer_unescaped = html.unescape(refer_body).strip() if refer_body else ""
    # WeChat may wrap the escaped nested appmsg in CDATA. Decode the entity layer
    # first, then remove that wrapper so the nested card is recognized as XML.
    cdata = re.fullmatch(r"<!\[CDATA\[(.*?)\]\]>", refer_unescaped, flags=re.S)
    if cdata:
        refer_unescaped = cdata.group(1).strip()
    for _ in range(2):
        if not refer_unescaped.startswith("&lt;"):
            break
        refer_unescaped = html.unescape(refer_unescaped).strip()
    nested_start = refer_unescaped.find("<appmsg")
    if nested_start > 0:
        # Group quote payloads can prefix the quoted appmsg with sender metadata.
        refer_unescaped = refer_unescaped[nested_start:]
    refer_md5 = None
    refer_content = ""
    refer_title = ""
    refer_url = ""
    refer_time = _xml_tag(refer, "createtime") if refer else ""
    if refer_body:
        mm = re.search(r'md5="([0-9a-fA-F]{32})"', refer_unescaped)
        if mm:
            refer_md5 = mm.group(1).lower()
        else:
            # 引用**表情包**时，refermsg 的 content 不是图片 XML，而是一串微信消息 ID：
            #   wxid_xxx:1792116875436:1:fb0dbcea25affabed88336bb23f520d1::0
            # 第 2 段是毫秒时间戳、第 4 段就是 md5。以前这串 ID 被当成正文喂给模型，
            # 于是「引用一张表情包问这是啥」只能看到一串 ID（2026-10-16 实测）。
            im = _REFER_ID_RE.match(refer_unescaped)
            if im:
                refer_md5 = im.group(2).lower()
                ts = im.group(1)
                refer_time = refer_time or (str(int(ts) // 1000) if len(ts) > 10 else ts)
            elif refer_unescaped.startswith("<"):
                # 引用的是链接/文件卡片：content 是转义后的 appmsg XML。
                # 必须解析出完整 <url>；以前把它当纯文本再 [:100] 截断，
                # 公众号长链接就只剩 https://mp.weixin.qq.c。
                refer_title = _xml_tag(refer_unescaped, "title")
                refer_url = html.unescape(
                    _xml_tag(refer_unescaped, "url")
                    or _xml_tag(refer_unescaped, "shareUrlOpen")
                    or _xml_tag(refer_unescaped, "shareUrlOriginal")
                    or ""
                ).strip()
            else:
                # 纯文本引用：**把被引用的原话带出来**。
                refer_content = refer_unescaped
    # 取 appmsg 自己的 <type> 前必须先剥掉 <refermsg>：微信有时把 <type>57</type> 放到
    # <refermsg> 之后，而 _xml_tag 只取第一个 <type>，会误抓被引用消息的类型
    # （43 视频 / 47 表情）→ kind 变成「卡片」→ @ 丢段（2026-11-08）
    _own = re.sub(r"<refermsg>.*?</refermsg>", "", text, flags=re.S)

    return {
        "kind": APPMSG_LABELS.get(_xml_tag(_own, "type"), "卡片"),
        "title": _xml_tag(text, "title"),
        "des": _xml_tag(text, "des"),
        "url": html.unescape(
            _xml_tag(text, "url") or _xml_tag(text, "dataurl") or ""
        ).strip(),
        "refer_author": _xml_tag(refer, "displayname") if refer else "",
        "refer_md5": refer_md5,
        "refer_content": refer_content,
        "refer_title": refer_title,
        "refer_url": refer_url,
        "refer_time": refer_time,
        # P13：被引用者的微信 ID（真实 wxid，来自 refermsg 元数据；可能为空）
        "refer_chatusr": _xml_tag(refer, "chatusr") if refer else "",
    }


def parse_record_items(raw: str) -> list[dict]:
    """解析「合并转发的聊天记录」里的 recorditem。

    微信把转发记录放在 `<recorditem><![CDATA[...]]></recorditem>`，里面的
    文字在 `<datadesc>`，图片只给 CDN 加密引用（cdnthumburl/cdnthumbkey），
    桥没有 CDN 下载协议。这里至少把文字条目和图片数量读出来，避免模型把
    `[图片]` 当成已经看过的内容。
    """
    if not raw or "<recorditem" not in raw:
        return []
    match = re.search(
        r"<recorditem><!\[CDATA\[(.*?)\]\]></recorditem>", raw, re.S
    )
    if not match:
        # 非 CDATA 的少数版本
        match = re.search(r"<recorditem>(.*?)</recorditem>", raw, re.S)
    if not match:
        return []
    record = match.group(1)
    items: list[dict] = []
    for block in re.findall(r"<dataitem\b.*?</dataitem>", record, re.S):
        def tag(name: str) -> str:
            m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
            return html.unescape(m.group(1)).strip() if m else ""

        datatype = tag("datatype")
        if not datatype:
            dbtype = re.search(r'<dataitem\b[^>]*datatype="([^"]+)"', block)
            datatype = dbtype.group(1) if dbtype else ""
        desc = tag("datadesc")
        sender = tag("sourcename") or "?"
        if not desc:
            desc = {
                "2": "[图片]",
                "3": "[语音]",
                "4": "[视频]",
                "5": "[文件]",
            }.get(datatype, f"[{datatype or '内容'}]")
        items.append({
            "datatype": datatype,
            "sender": sender,
            "desc": desc,
            "src_time": tag("srcMsgCreateTime") or tag("sourcetime"),
        })
    return items


def file_segments(m: dict, group_id: str, wx, raw: str, ext_sink: dict | None = None) -> list[dict]:
    """文件消息：白名单校验 → 下载 → 入库；不把二进制送给模型。

    入库成功时把**结构化文件事实**（P13：句柄/名称/大小/哈希/群归属）写入
    `ext_sink["file"]`，供事件 `pero_ext` 上报；正文仍是占位文本。
    """
    info = parse_appmsg(raw) or {}
    name = (info.get("title") or "").strip() or "file"
    verdict, reason = FILE_LIBRARY.classify(name)
    if verdict == "reject":
        log.info("[文件] 拒收 %s：%s", name, reason)
        return [{"type": "text", "data": {"text": f"[文件] {name}（拒收：{reason}）"}}]
    if verdict != "accept":
        # verify/sniff：扩展名只是初筛，下载后由 FileLibrary.add 做有界内容校验。
        log.info("[文件] 待内容校验 %s（%s）", name, verdict)
    size_text = _xml_tag(raw, "totallen")
    expected_size = int(size_text) if size_text.isdigit() else 0
    expected_md5 = _xml_tag(raw, "filemd5") or _xml_tag(raw, "md5")
    source_raw = m.get("raw") or {}
    occurred_at = source_raw.get("create_time") or source_raw.get("createTime") or 0
    path = wx.download_file(group_id, m.get("local_id"), name,
                            expected_size=expected_size, expected_md5=expected_md5,
                            occurred_at=occurred_at)
    if not path:
        log.info("[文件] 未找到本地副本：%s", name)
        return [{"type": "text", "data": {"text": f"[文件] {name}（未收到完整文件，未入库）"}}]
    try:
        item = FILE_LIBRARY.add(
            path,
            name,
            group=group_id,
            sender=str(m.get("sender_name") or ""),
            source_local_id=m.get("local_id"),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("[文件] 入库失败 %s：%s", name, e)
        return [{"type": "text", "data": {"text": f"[文件] {name}（未入库：{e}）"}}]
    log.info("[文件] 已入库 #%s（%s，%d 字节）", item["id"], item["name"], item["size"])
    if ext_sink is not None:
        ext_sink["file"] = {
            "v": 1,
            "file_id": str(item.get("id") or "")[:40],
            "name": str(item.get("name") or "")[:200],
            "size": int(item.get("size") or 0),
            "sha256": str(item.get("sha256") or "")[:64],
            "group_wxid": str(group_id or "")[:64],
            "sender_name": str(item.get("sender") or "")[:60],
            "source_local_id": str(item.get("source_local_id") or "")[:64],
            "source": "bridge_file_library",
        }
    return [{
        "type": "text",
        "data": {
            "text": f"[文件 #{item['id']}] {item['name']}（已存入文件库，可用 list_files / read_file 按需读取）"
        },
    }]


def card_segments(
    raw: str,
    group_id: str,
    wx,
    max_bytes: int,
    nicknames: list[str],
    self_id: int,
) -> list[dict]:
    """「文件/链接/卡片」→ 消息段。

    最关键的是**引用回复**：正文在 `<title>` 里，必须按文本再走一遍 `build_segments`
    做 @ 识别，否则整条消息被吞掉。被引用的图片如果能解出来，也一并带上 ——
    不然模型回答不了「这是什么图」。
    """
    info = parse_appmsg(raw)
    if info is None:
        return [{"type": "text", "data": {"text": "[卡片]"}}]

    segs: list[dict] = []

    # 被引用的图片：按 md5 / 时间戳找回 local_id 再解密。
    # 注意只在 refer_md5 存在时查 —— 引用**文本**时也带 createtime，之前拿它去
    # 查图必然落空，白付一次查库 + 打一行误导性的「图片拿不到」日志。
    missing_image = False
    if info["refer_md5"]:
        lid = wx.image_local_id_by_ref(group_id, info["refer_md5"], info["refer_time"])
        img = image_segment(wx.image_path(group_id, lid), max_bytes) if lid else None
        src = f"微信图片库(local_id={lid})"
        if not img:
            # ① 被引用的多半是**表情包**：表情不在图片表里，但它的静态帧早就按 md5
            #    缓存在 cache/emoji/ 了，直接用（2026-10-16 修：引用表情包看不到图）。
            cached = cached_sticker(info["refer_md5"])
            if cached:
                img = image_segment(str(cached), max_bytes)
                src = f"表情缓存({cached.name})"
        if not img:
            # ② 普通图片缓存（md5 键）：收到图时按本地 packed_info 的 md5 存了一份。
            cached = cached_image(info["refer_md5"], group_id)
            if cached:
                img = image_segment(str(cached), max_bytes)
                src = f"图片缓存(md5 {cached.name})"
        if not img:
            # ③ 图片缓存（时间键）：引用 XML 里的 md5 是**CDN 上的原图 md5**，与本地
            #    packed_info 的 md5 往往不同（2026-10-16 实测），但 `<createtime>`
            #    与本地行的 create_time 严格一致 —— 它才是可靠检索键。
            cached = cached_image_by_time(info["refer_time"], group_wxid=group_id)
            if cached:
                img = image_segment(str(cached), max_bytes)
                src = f"图片缓存(时间 {cached.name})"
        if img:
            segs.extend(img)
            log.info(
                "[卡片] 已附上被引用的图片（md5=%s time=%s 来源=%s）",
                info["refer_md5"],
                info["refer_time"],
                src,
            )
        else:
            missing_image = True
            log.info(
                "[卡片] 被引用的图片拿不到本地副本（md5=%s time=%s）",
                info["refer_md5"],
                info["refer_time"],
            )

    title = info["title"] or ""
    if info["kind"] == "引用回复":
        if info["refer_author"]:
            what = "图片" if info["refer_md5"] else "消息"
            refer_url = (info.get("refer_url") or "").strip()
            refer_title = (info.get("refer_title") or "").strip()
            quoted = (info.get("refer_content") or "").replace("\n", " ").strip()
            if refer_url:
                quoted = f"{refer_title}（{refer_url}）".strip()
            elif refer_title:
                quoted = refer_title
            if missing_image:
                # 不要把「找不到」留白让模型自己猜：它会拿最近一张图顶上，
                # 实测会把别的图说成被引用的图（2026-10-23）。
                title = (
                    f"（引用 {info['refer_author']} 的图片，"
                    f"但本机没有找到原图，无法查看；不要猜测内容）{title}"
                )
            elif quoted:
                # 链接必须完整保留；只有普通文字引用才截到 100 字。
                shown = quoted if refer_url else quoted[:100]
                title = (
                    f"（引用 {info['refer_author']} 的{what}："
                    f"「{shown}」）{title}"
                )
            else:
                title = f"（引用 {info['refer_author']} 的{what}）{title}"
        # 引用的是**机器人自己的消息** → 视同 @（补一个 at 段）。
        # 为什么必须补：微信里「引用回复」是最自然的跟它说话的方式，但引用本身
        # 不带 @ —— 窗口一关（30s 滑动）引用了也不会理（实测：它刚答完 KMP，
        # 小泽布尔引用了说「太难了」，机器人装死）。补 at 后：开窗 + 必回。
        if info["refer_author"] and info["refer_author"] in nicknames:
            segs.append({"type": "at", "data": {"qq": str(self_id)}})
            log.info("[卡片] 引用的是机器人自己的消息 → 视同 @（开窗必回）")
        # 走文本通道 → @ 会被识别成 at 段，窗口也才开得起来
        segs.extend(build_segments(title, nicknames, self_id))
    else:
        if info["kind"] == "聊天记录":
            items = parse_record_items(raw)
            if items:
                lines = [
                    f"{it['sender']}: {it['desc'][:160]}"
                    for it in items[:10]
                ]
                images = sum(
                    1 for it in items
                    if it["datatype"] == "2" or it["desc"].startswith("[图片]")
                )
                label = f"[聊天记录] {info['title'] or '聊天记录'}"
                if lines:
                    label += " — " + " / ".join(lines)
                label += (
                    f"（转发记录共{len(items)}条"
                    + (f"，含{images}张图片" if images else "")
                    + "；转发记录内的图片内容无法直接读取，不要猜测）"
                )
                segs.append({"type": "text", "data": {"text": label}})
                return segs
        label = f"[{info['kind']}] {title}".strip()
        if info["des"]:
            label += f" — {info['des'][:120]}"
        if info.get("url"):
            label += f"\nURL: {info['url'][:1200]}"
        segs.append({"type": "text", "data": {"text": label}})
    return segs


def image_segment(path: str | None, max_bytes: int) -> list[dict] | None:
    """把解密后的图片文件转成 OneBot v11 的 image 段（base64）。

    为什么用 base64 而不是 `file://`：
        AstrBot 跑在容器里，**宿主机路径它读不到**（除非再配一次目录挂载）。
        本项目已经因为「挂载漏配」踩过一次坑（插件目录），这里不再依赖挂载。
        代价只是体积膨胀约 1/3，本地 WS 传输可忽略。
    """
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            payload = f.read()
        max_edge = int(MEDIA_OPTS.get("image_max_edge", 0))
        if max_edge > 0:
            from PIL import Image, ImageOps

            with Image.open(BytesIO(payload)) as raw:
                if max(raw.size) > max_edge:
                    frame = ImageOps.exif_transpose(raw)
                    frame.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                    if frame.mode != "RGB":
                        rgb = Image.new("RGB", frame.size, "white")
                        if frame.mode == "RGBA":
                            rgb.paste(frame, mask=frame.getchannel("A"))
                        else:
                            rgb.paste(frame.convert("RGB"))
                        frame = rgb
                    with BytesIO() as output:
                        frame.save(output, format="JPEG", quality=84, optimize=True)
                        payload = output.getvalue()
        if len(payload) > max_bytes:
            log.warning("图片过大（%d 字节 > 上限 %d），改为占位文本：%s", len(payload), max_bytes, path)
            return None
        b64 = base64.b64encode(payload).decode("ascii")
    except OSError as e:
        log.warning("读取图片失败 %s: %s", path, e)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("图片缩放失败 %s: %s", path, e)
        return None
    return [{"type": "image", "data": {"file": f"base64://{b64}"}}]


def build_media_segments(
    m: dict,
    group_id: str,
    wx,
    max_bytes: int,
    nicknames: list[str],
    self_id: int,
    ext_sink: dict | None = None,
) -> list[dict] | None:
    """非文本消息 → OneBot 消息段。返回 None 表示这类消息不上报。

    图片：解密成本地文件后以 base64 上报，模型能直接看图。
    卡片/引用回复：解析 appmsg，抽出正文（引用回复的正文里可能带 @）。
    **动画表情：也当图片处理** —— 从 XML 的 cdnurl 取原文件，动图只取第一帧
    （见 wechat_client.WeChatClient.sticker_frame）；取不到才退化为 `[表情包]`。
    语音：拿不到原始文件，退化为占位文本（不丢信息，也不发噪音）。
    """
    mtype = str(m.get("type") or "")

    if "图片" in mtype:
        path = wx.image_path(group_id, m.get("local_id"))
        seq = None
        if path:
            # 存进索引：每群独立编号、保留所有，供引用旧图和 AI 按编号查看
            try:
                ref = wx.cache_image_for_reference(
                    group_id, m.get("local_id"), path,
                    MEDIA_OPTS.get("image_cache_keep", -1),
                    sender=m.get("sender_name", ""),
                    context="",
                )
                if ref:
                    seq = ref.get("seq")
                    log.info("[图片] 已缓存 #%s（local_id=%s）", seq, m.get("local_id"))
            except Exception as e:  # noqa: BLE001
                log.debug("图片缓存失败（local_id=%s）: %s", m.get("local_id"), e)
        segs = image_segment(path, max_bytes)
        if segs:
            if seq:
                return [{"type": "text", "data": {"text": f"[图片 #{seq}]"}}] + segs
            return segs
        return [{"type": "text", "data": {"text": "[图片（本机没有可解密的副本）]"}}]

    if "表情" in mtype:
        if MEDIA_OPTS.get("fetch_stickers", True):
            try:
                frame = wx.sticker_frame(group_id, m.get("local_id"))
                segs = image_segment(frame, max_bytes)
                if segs:
                    seq = None
                    if frame and int(MEDIA_OPTS.get("image_cache_keep", -1)) != 0:
                        raw = m.get("raw") or {}
                        content = str(raw.get("content") or m.get("text") or "")
                        md5s = re.findall(r'md5="([0-9a-fA-F]{32})"', content)
                        ref = wx.cache_file_for_reference(
                            group_id,
                            frame,
                            sender=m.get("sender_name", ""),
                            context="",
                            create_time=m.get("create_time"),
                            md5s=md5s,
                            local_id=m.get("local_id"),
                        )
                        if ref:
                            seq = ref.get("seq")
                    if seq:
                        return [{"type": "text", "data": {"text": f"[图片 #{seq}]"}}] + segs
                    return segs
            except Exception as e:  # noqa: BLE001
                # 反过来兜底：取图这条路炸了也只退化为占位，
                # **不能**把整条消息丢掉（那是「不认识表情包」的成因）
                log.warning("表情包取图异常（local_id=%s）: %s", m.get("local_id"), e)
        return [{"type": "text", "data": {"text": "[表情包]"}}]

    if "卡片" in mtype or "链接" in mtype or "文件" in mtype:
        raw_card = m.get("text") or ""
        info = parse_appmsg(raw_card)
        if info and info.get("kind") == "文件":
            return file_segments(m, group_id, wx, raw_card, ext_sink=ext_sink)
        return card_segments(
            raw_card, group_id, wx, max_bytes, nicknames, self_id
        )

    for key, placeholder in MEDIA_PLACEHOLDER.items():
        if key in mtype:
            return [{"type": "text", "data": {"text": placeholder}}]

    return None


def clean_markdown(text: str) -> str:
    """微信不渲染 Markdown，去掉标记避免回复里露出星号。"""
    t = text
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)              # **粗体**
    t = re.sub(r"(?<![*\w])\*([^*\n]+?)\*(?![*\w])", r"\1", t)  # *斜体*
    t = re.sub(r"`([^`\n]+)`", r"\1", t)                # `代码`
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.MULTILINE)  # 标题
    t = re.sub(r"^>\s*", "", t, flags=re.MULTILINE)      # 引用
    return t


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            user_cfg = json.load(f)
        for section, values in user_cfg.items():
            if isinstance(values, dict) and isinstance(cfg.get(section), dict):
                cfg[section].update(values)
            else:
                cfg[section] = values
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"[i] 已生成默认配置: {CONFIG_PATH}")
    return cfg


async def main() -> int:
    from bridge_lock import BridgeLock
    ownership=BridgeLock()
    if not ownership.acquire():
        log.error('此 webot 目录已有桥实例运行，请先按原方式停止。')
        return 1
    try:return await _main()
    finally:ownership.close()


async def _main() -> int:
    from bridge_lock import require_client_running
    try:require_client_running()
    except Exception:
        log.error('请先手动启动微信并登录；桥不会启动微信客户端。')
        return 1
    cfg = load_config()

    logging.basicConfig(
        level=getattr(logging, str(cfg["logging"].get("level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # log 已在模块级定义（模块级函数也要用），这里不再重复创建

    # 放行 wechatauto 自身的日志。wechatauto_level=DEBUG 时会打出侧栏查找、
    # OCR 兜底、open_chat 等细节 —— 用于证实「已在目标群时不发生切换」。
    _wx_level = str(cfg["logging"].get("wechatauto_level", "INFO")).upper()
    logging.getLogger("wechatauto").setLevel(getattr(logging, _wx_level, logging.INFO))
    log.info("wechatauto 日志级别: %s", _wx_level)

    # ── 微信侧 ────────────────────────────────────────
    # 模块级媒体开关（build_media_segments 要用，见 MEDIA_OPTS 的说明）
    init_media_opts(cfg)
    log.info(
        "媒体开关：发送图片=%s 表情包当图=%s 单图上限=%s 字节",
        bool(cfg["wechat"].get("send_images", True)),
        MEDIA_OPTS.get("fetch_stickers"),
        cfg["wechat"].get("image_max_bytes", 3000000),
    )
    # 必须在导入/实例化 wechatauto 之前设置节奏档位
    os.environ["WECHATAUTO_RHYTHM"] = str(cfg["wechat"].get("rhythm", "fast"))
    log.info("拟人节奏层档位: %s", os.environ["WECHATAUTO_RHYTHM"])

    try:
        wx = WeChatClient(
            bot_nickname=cfg["wechat"].get("bot_nickname", ""),
            db_dir=cfg["wechat"].get("db_dir") or None,
            verify_send=bool(cfg["wechat"].get("verify_send", False)),
            expected_wxid=str(cfg["wechat"].get("self_wxid") or ""),
        )
    except Exception as e:  # noqa: BLE001
        log.error("初始化微信失败（微信是否已登录并保持运行？）: %s", e)
        return 1

    identities = IdentityDirectory(Path(__file__).parent / 'cache' / 'identity.sqlite3', wx.self_wxid)
    identity_seeded=set()

    # @ 识别别名表。
    # 微信数据库里的 @ 是纯文本、没有结构化字段，只能靠昵称匹配 —— 所以：
    #   ① 第一来源是**数据库里实时读到的自己昵称**（在微信里改名后无需改配置）；
    #   ② 再叠加 config 里手写的别名（历史昵称 / 群昵称等，长别名优先匹配）。
    # 曾经踩过：昵称在微信里从 hitokami 改成大肥鱼，而配置没跟着改，
    # 导致 @ 没被识别成 at 段 → 插件判定「@ 的是别人」→ 直接不回复。
    mention_aliases = [wx.self_name] + [
        n
        for n in (
            [cfg["wechat"].get("bot_nickname", "")]
            + list(cfg["wechat"].get("bot_nicknames") or [])
        )
        if n
    ]
    mention_aliases = list(
        dict.fromkeys(n.strip() for n in mention_aliases if n and n.strip() and n != "unknown")
    )
    log.info("自己昵称: %s ｜ @ 识别别名: %s", wx.self_name, mention_aliases)

    # 群映射：优先使用 config 里显式配置的 {群ID: 界面显示名}
    # （微信未改名的群在界面上显示为成员昵称拼接，无法可靠匹配，
    #   因此发送必须依赖一个确定的「群名称」）
    groups_cfg = cfg["wechat"].get("groups") or {}
    if groups_cfg:
        groups = [{"id": gid, "name": name} for gid, name in groups_cfg.items()]
    else:
        groups = wx.groups()
        whitelist = set(cfg["wechat"].get("group_whitelist") or [])
        if whitelist:
            groups = [g for g in groups if g["name"] in whitelist or g["id"] in whitelist]
    if not groups:
        log.error("没有可监听的群，请在 config.json 的 wechat.groups 中配置 {群ID: 群名称}")
        return 1

    log.info("监听 %d 个群: %s", len(groups), [g["name"] for g in groups][:10])

    # 启动时对齐水位，避免把历史消息当新消息。
    # 严格读取：单个群查不到水位就**跳过这个群**（不回退 0，否则整段历史会被当新消息），
    # 也不因为一个群的异常中止全部会话的启动。
    last_seq: dict[str, int] = {}
    skipped_groups = 0
    for g in groups:
        # 短暂重试（数据库可能启动瞬间被锁）；仍失败则**跳过该群**，绝不回退 0。
        for attempt in range(3):
            try:
                last_seq[g["id"]] = int(wx.latest_seq(g["id"], strict=True))
                break
            except Exception as exc:  # noqa: BLE001 - 单群失败不影响其它会话
                if attempt < 2:
                    await asyncio.sleep(0.5)
                    continue
                skipped_groups += 1
                log.warning(
                    "群水位读取失败（%s / %s）：本次跳过该群，不回退到 0",
                    g["name"], type(exc).__name__,
                )
    log.info(
        "水位基线已对齐（群 %d 个%s）",
        len(last_seq),
        f"，跳过 {skipped_groups} 个" if skipped_groups else "",
    )
    # 群列表回传到本机（贝蒂查群用）：后台执行，失败只记日志，不影响启动（group_sync 插件）
    try:
        from group_sync import push_groups
        _all_groups = wx.groups() or []
        asyncio.create_task(asyncio.to_thread(
            push_groups, cfg, _all_groups or groups,
            {g["id"] for g in groups}, wx.self_name,
        ))
    except Exception as e:  # noqa: BLE001
        log.warning("[群同步] 未能启动回传: %s", e)

    # ── AstrBot 侧 ────────────────────────────────────
    async def _refresh_layout_after_switch(wx):
        """显示拓扑切换后，主动预热一次布局探测。

        为什么：切到虚拟屏 / 切回内屏会改变窗口分辨率与 DPI，wechatauto 的布局缓存
        随之失效；若等到下一条要发的消息才发现，输入框探测会连续失败并触发一次完整
        OCR 重校准 —— 实测一次发送被拖到 **145 秒**（2026-10-09 10:36:15）。
        这里在后台等切换完成，先把这份开销付掉。
        """
        await asyncio.sleep(14)
        try:
            gui = wx.gui
            gui._auto_recalibrated = False        # 允许再次自动重校准
            gui._update_render_rect()
            box = gui.get_input_box()
            log.info(
                "[屏幕] 切换后布局预热：输入框 %s",
                "已定位" if box else "未定位（下一条消息会重校准）",
            )
        except Exception as e:  # noqa: BLE001
            log.debug("[屏幕] 切换后布局预热失败: %s", e)

    website_lock = asyncio.Lock()
    # R01-03：微信窗口操作的**统一串行调度**。目标定位、文字、@、逐张图片与
    # 屏幕控制都必须在同一把锁内完成；v1/v2 批量发送因此整批连续执行，
    # 不会被普通文字/旧表情动作插入。只读查询（文件/身份/缓存/网页）不占锁。
    gui_lock = asyncio.Lock()
    gui_actions = frozenset({
        "wake_screen", "sleep_screen",
        "sticker_send", "text_sticker",
        "pero_media_send_v1", "pero_media_send_v2",
        "send_group_msg", "send_private_msg", "send_msg", "send_group_forward_msg",
    })
    private_cfg = cfg.get("private_chat") or {}
    pero_url = str(private_cfg.get("pero_url") or "http://localhost:8765")
    internal_token = os.environ.get(str(private_cfg.get("token_env") or "PERO_MANAGEMENT_TOKEN"), "").strip()
    private_account_id = str(cfg["astrbot"].get("self_id", 10001))
    authorized_privates: dict[str, dict] = {}
    max_mentions = max(1, int(cfg["wechat"].get("max_mentions", 3)))

    async def _dispatch(action: str, params: dict):
        if action in ("read_website", "browser_search"):
            # No shell, arbitrary script, desktop input or file access is exposed.
            if website_lock.locked():
                return {"ok": False, "error": "宿主机浏览器正忙，请稍后再试"}
            async with website_lock:
                if action == "browser_search":
                    return await browser_search(params.get("query", ""), cfg.get("website") or {}, params.get("top_k", 6))
                url = params.get("url", "")
                if is_wechat_article(url):
                    return await read_wechat_article(url)
                return await read_website(url, cfg.get("website") or {})
        if action == "get_login_info":
            return {"user_id": cfg["astrbot"].get("self_id", 10001), "nickname": wx.self_name}
        if action == "get_version_info":
            return {
                "app_name": "webot-bridge",
                "app_version": "0.1.0",
                "protocol_version": "v11",
                "private_target_protocol": "conversation_type=private,target_id=bridge_user_id",
                "private_media_actions": ["pero_media_send_v1", "pero_media_send_v2"],
            }
        if action == "get_status":
            return {"online": True, "good": True}

        # ── 屏幕控制（本项目的私有动作，不是 OneBot 标准）──────────
        # 为什么放在桥：插件跑在 Linux 容器里，碰不到宿主机显示器；桥在 Windows 上，
        # 由它去调 scripts\screen-off.ps1 最直接。
        #   wake_screen  → 开屏（切回内屏）
        #   sleep_screen → 熄屏（虚拟屏，机器人继续跑；@ 一下即可唤醒）
        # 都用 Popen 起独立进程：熄屏脚本会**阻塞等待真人输入**（最长 10 小时），
        # 绝不能占住桥的事件循环。
        if action in ("wake_screen", "sleep_screen"):
            script = r"c:\webot\scripts\screen-off.ps1"
            args = ["-Restore"] if action == "wake_screen" else ["-Seconds", "36000"]
            try:
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", script, *args],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    close_fds=True,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("[屏幕] %s 启动失败: %s", action, e)
                return {"ok": False, "error": str(e)}
            log.info("[屏幕] %s 已触发（独立进程）", "开屏" if action == "wake_screen" else "熄屏")
            # 切换会打乱窗口几何 → 后台等它切完，先预热一次布局探测
            asyncio.create_task(_refresh_layout_after_switch(wx))
            return {"ok": True, "action": action}
        if action == "get_group_list":
            return [{"group_id": _fake_id(g["id"]), "group_name": g["name"]} for g in groups]
        if action in ("get_identity_directory", "get_group_member_list"):
            # 给插件注入「本群所有可 @ 成员」用；只返回显示名（微信 @ 面板按名字选人）。
            gid = params.get("group_id")
            wxid = _wxid_of(gid)
            if not wxid:
                return []
            identities.update_members(wxid, wx.db.get_group_members(wxid) or [])
            if wxid not in identity_seeded:
                for row in wx.db.get_messages(wxid,1000,0) or []:
                    content=row.get('content') or ''
                    identities.observe_reference(wxid,content if isinstance(content,str) else _zstd_text(content))
                identity_seeded.add(wxid)
            directory = identities.directory(wxid)
            directory['session'] = bot.bridge_session
            if action == "get_identity_directory":
                return directory
            return [{"user_id": m['user_id'], "nickname": m['nickname'], "card": ""}
                    for m in directory['members']]
        if action == 'get_message_identity':
            return identities.message(str(params.get('session') or bot.bridge_session), params.get('message_id', 0))
        if action in ("get_cached_image", "list_cached_images"):
            gid = params.get("group_id")
            wxid = _wxid_of(gid)
            if not wxid:
                return {"ok": False, "error": "group_not_found"}
            if action == "get_cached_image":
                try:
                    seq = int(params.get("image_id") or 0)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "bad_image_id"}
                item = wx.get_cached_image(wxid, seq)
                if not item:
                    return {"ok": False, "error": "not_found"}
                try:
                    payload = Path(item["file"]).read_bytes()
                except OSError as e:
                    return {"ok": False, "error": f"read_failed: {e}"}
                return {
                    "ok": True,
                    "id": seq,
                    "name": Path(item['file']).name,
                    "sender": item.get("sender") or "",
                    "create_time": int(item.get("create_time") or 0),
                    "context": str(item.get("context") or "")[:200],
                    "mime": "image/jpeg",
                    "data": base64.b64encode(payload).decode("ascii"),
                }
            try:
                limit = int(params.get("limit") or 20)
            except (TypeError, ValueError):
                limit = 20
            result = wx.list_cached_images(
                wxid,
                sender=str(params.get("sender") or ""),
                since=params.get("since"),
                limit=limit,
            )
            items = []
            for item in result.get("items") or []:
                ts = int(item.get("create_time") or 0)
                items.append({
                    "id": item.get("seq"),
                    "sender": item.get("sender") or "",
                    "create_time": ts,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "",
                    "context": str(item.get("context") or "")[:120],
                })
            return {"ok": True, "items": items, "has_more": bool(result.get("has_more"))}
        if action == "list_stickers":
            try:
                sticker_limit = int(params.get("limit") or 35)
            except (TypeError, ValueError):
                sticker_limit = 35
            sticker_limit = max(1, min(sticker_limit, 200))
            return {
                "ok": True,
                "items": STICKERS.search(
                    query=str(params.get("query") or ""),
                    emotion=str(params.get("emotion") or ""),
                    scene=str(params.get("scene") or ""),
                    limit=sticker_limit,
                    include_blocked=bool(params.get("include_blocked")),
                ),
            }
        if action == "save_sticker":
            gid = params.get("group_id")
            wxid = _wxid_of(gid)
            if not wxid:
                return {"ok": False, "error": "group_not_found"}
            try:
                seq = int(params.get("image_id") or 0)
            except (TypeError, ValueError):
                return {"ok": False, "error": "bad_image_id"}
            src = wx.get_cached_image(wxid, seq)
            if not src:
                return {"ok": False, "error": "image_not_found"}
            try:
                saved = STICKERS.add(
                    src["file"],
                    name=str(params.get("name") or "表情"),
                    emotion=str(params.get("emotion") or ""),
                    scenes=params.get("scenes"),
                    tags=params.get("tags"),
                    description=str(params.get("description") or ""),
                    group=_name_of(gid) or gid or "",
                    sender=str(src.get("sender") or ""),
                    image_id=seq,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("表情入库失败: %s", e)
                return {"ok": False, "error": str(e)}
            log.info("[表情] 已入库 %s（%s）", saved.get("id"), saved.get("name"))
            return {"ok": True, "sticker": saved}
        if action == "rename_sticker":
            try:
                renamed = STICKERS.rename(
                    str(params.get("sticker_id") or ""),
                    name=str(params.get("name") or ""),
                    emotion=str(params.get("emotion") or ""),
                    description=str(params.get("description") or ""),
                    add_tags=params.get("add_tags"),
                    remove_tags=params.get("remove_tags"),
                )
            except KeyError:
                return {"ok": False, "error": "sticker_not_found"}
            except Exception as e:  # noqa: BLE001
                log.warning("表情改名失败: %s", e)
                return {"ok": False, "error": str(e)}
            log.info(
                "[表情] 已改名 %s → %s（标签：%s）",
                renamed.get("id"),
                renamed.get("name"),
                "、".join(renamed.get("tags") or []) or "-",
            )
            return {"ok": True, "sticker": renamed}
        if action == "sticker_send":
            gid = params.get("group_id")
            name = _name_of(gid)
            item = STICKERS.find(str(params.get("sticker_id") or "").strip().lstrip("#"))
            if not name:
                return {"ok": False, "error": "group_not_found"}
            if not item:
                return {"ok": False, "error": "sticker_not_found"}
            if STICKERS.is_blocked(item):
                return {
                    "ok": False,
                    "error": "sticker_blocked",
                    "detail": "这张被标记过「不要发」，换一张",
                }
            outcome = await asyncio.to_thread(
                wx.send_image, name, item["path"]
            )
            if not outcome:
                state = outcome.state if isinstance(outcome, SendResult) else "unknown"
                detail = outcome.detail if isinstance(outcome, SendResult) else "发送失败"
                return {"ok": False, "state": state, "error": detail}
            STICKERS.mark_used(item["id"])
            return {
                "ok": True,
                "sticker_id": item["id"],
                "name": item.get("name") or "",
            }
        if action == "text_sticker":
            gid = params.get("group_id")
            name = _name_of(gid)
            target = str(
                params.get("sticker") or params.get("sticker_id") or ""
            ).strip().lstrip("#")
            if not name:
                return {"ok": False, "error": "group_not_found", "sent_text": False}
            item = STICKERS.find(target)
            # 只认编号（st_xxxx）。以前名字也走 search(limit=1) 兜底，结果永远命中
            # 编号最小的那张（正反馈，全群只见过一张），已废弃：
            # 名字只在 list_stickers 里给模型挑，发送必须给编号（2026-11-08）。
            if not item:
                return {"ok": False, "error": "sticker_not_found", "sent_text": False}
            if STICKERS.is_blocked(item):
                return {"ok": False, "error": "sticker_blocked", "sent_text": False}
            body = str(params.get("text") or "")
            meaningful = re.sub(r"@[^\s]+", "", body)
            meaningful = re.sub(r"[\s。.!！?？~～,，、;；:：]+", "", meaningful)
            if not meaningful:
                # 模型只写了 @/标记时，桥侧再兜一层，绝不发空 @。
                body = (
                    body + f" 好嘞，给你配一个{item.get('name') or ''}表情。"
                ).strip()
            log.info(
                "[表情] text_sticker 目标=%s 正文=%r",
                item.get("id"),
                body[:200],
            )
            wxid = _wxid_of(gid)
            names = wx.member_names(wxid) if wxid else set()
            parts = []
            if "@" in body and names:
                parts = parse_mentions(
                    body,
                    names,
                    int(cfg["wechat"].get("max_mentions", 3) or 3),
                )
            if parts:
                # parse_mentions 输出的是 send_at_multi 需要的
                # {"type":"at","name":...} / {"type":"text","text":...}，
                # 不能用 build_segments 的 OneBot data 段。
                await asyncio.to_thread(wx.ensure_visible)
                at_result = await asyncio.to_thread(wx.send_at_multi, name, parts)
                sent_text = bool(at_result)
                if not sent_text:
                    return {
                        "ok": False,
                        "error": "文字发送失败",
                        "sent_text": False,
                        "sticker_id": item["id"],
                    }
                # 紧跟文字发图：坐标刚由 send_text 缓存，跳过重复探测省 0.2–0.5 秒
                image_result = await asyncio.to_thread(
                    lambda: wx.send_image(name, item["path"], probe=False)
                )
                if not image_result:
                    return {
                        "ok": False,
                        "error": f"图片发送失败：{image_result.detail or image_result.state}",
                        "sent_text": True,
                        "sticker_id": item["id"],
                    }
                outcome = {"ok": True, "sent_text": True}
            else:
                outcome = await asyncio.to_thread(
                    wx.send_text_and_image,
                    name,
                    body,
                    item["path"],
                )
            if not outcome.get("ok"):
                return {
                    "ok": False,
                    "error": outcome.get("error") or "发送失败",
                    "sent_text": bool(outcome.get("sent_text")),
                    "sticker_id": item["id"],
                }
            STICKERS.mark_used(item["id"])
            return {
                "ok": True,
                "sent_text": bool(outcome.get("sent_text")),
                "sticker_id": item["id"],
                "name": item.get("name") or "",
            }
        if action == "pero_media_send_v2":
            # Pero 专用（P12）：文字 + 最多 6 张图，一次业务批次。
            # - 图片只来自 action 内联 Base64（不查 STICKERS 库、不收磁盘路径）；
            # - 文字先确认 sent 才逐张发图；任一张 failed/unknown 即停止，
            #   后续标 skipped，绝不自动重发；
            # - 所有校验（数量/Base64/类型/大小/临时文件）在 GUI 操作前完成；
            # - 只清理本次临时文件，不动源图或目录。
            gid = ("private:" + str(params.get("target_id", params.get("user_id")))) if params.get("conversation_type") == "private" or params.get("user_id") is not None else params.get("group_id")
            name, target_wxid = _target_of(params)
            raw_images = params.get("images")
            raw_images = raw_images if isinstance(raw_images, list) else []

            def _derive_batch(text_state, states):
                """整批状态只由**已验证的文字与逐项状态**推导，不采纳上游自报。"""

                states = list(states)
                total = 1 + len(states)
                confirmed = (1 if text_state == "sent" else 0) + sum(
                    1 for s in states if s == "sent"
                )
                failed = (1 if text_state == "failed" else 0) + sum(
                    1 for s in states if s == "failed"
                )
                uncertain = (1 if text_state == "unknown" else 0) + sum(
                    1 for s in states if s == "unknown"
                )
                if confirmed == total:
                    return "sent"
                if confirmed > 0:
                    return "partial"
                if uncertain > 0:
                    return "unknown"
                if failed > 0:
                    return "failed"
                return "unknown"

            def _batch_reply(text_state, states, evidence, error="", errors=None):
                states = list(states)
                errors = list(errors or [""] * len(states))
                batch_state = _derive_batch(text_state, states)
                ok = text_state == "sent" and bool(states) and all(
                    s == "sent" for s in states
                )
                return {
                    "ok": ok,
                    "error": "" if ok else (error or f"文字{text_state}/{batch_state}"),
                    "text_state": text_state,
                    "batch_state": batch_state,
                    "images": [
                        {
                            "index": index,
                            "state": states[index] if index < len(states) else "skipped",
                            "asset_id": (
                                str(raw_images[index].get("asset_id") or "")[:80]
                                if isinstance(raw_images[index], dict)
                                else ""
                            ),
                            "error": (
                                str(errors[index] or "")[:120]
                                if index < len(errors)
                                else ""
                            ),
                        }
                        for index in range(len(raw_images))
                    ],
                    "evidence": evidence[:200],
                }

            if not name:
                return _batch_reply(
                    "unknown", ["skipped"] * len(raw_images),
                    "group_not_found", "group_not_found",
                    ["group_not_found"] * len(raw_images),
                )
            body = str(params.get("text") or "")
            business_id = str(params.get("action_id") or "")[:80]
            if not business_id:
                return _batch_reply(
                    "unknown", ["skipped"] * len(raw_images),
                    "missing_action_id", "missing_action_id",
                    ["missing_action_id"] * len(raw_images),
                )
            if not 1 <= len(raw_images) <= 6:
                return _batch_reply(
                    "unknown", ["skipped"] * len(raw_images),
                    "invalid_batch_size", "invalid_batch_size",
                    ["invalid_batch_size"] * len(raw_images),
                )
            if not body.strip():
                return _batch_reply(
                    "unknown", ["skipped"] * len(raw_images),
                    "empty_text", "empty_text",
                    ["empty_text"] * len(raw_images),
                )
            dedup_key = (str(gid), name, business_id)
            if dedup_key in _PERO_MEDIA_DEDUP:
                return _batch_reply(
                    "unknown", ["skipped"] * len(raw_images),
                    "重复业务动作，未再次执行", "duplicate_action",
                    ["duplicate_action"] * len(raw_images),
                )
            if len(_PERO_MEDIA_DEDUP) >= _PERO_MEDIA_DEDUP_MAX:
                _PERO_MEDIA_DEDUP.popitem(last=False)
            _PERO_MEDIA_DEDUP[dedup_key] = time.time()
            try:
                max_bytes = int(
                    (cfg.get("pero_media") or {}).get("max_image_bytes", 10_000_000)
                    or 10_000_000
                )
            except (TypeError, ValueError):
                max_bytes = 10_000_000
            try:
                max_total = int(
                    (cfg.get("pero_media") or {}).get("max_batch_bytes", 15_000_000)
                    or 15_000_000
                )
            except (TypeError, ValueError):
                max_total = 15_000_000
            media_dir = Path(__file__).resolve().parent / "cache" / "pero_media"
            media_dir.mkdir(parents=True, exist_ok=True)
            tmp_paths = []
            total_bytes = 0
            try:
                for index, item in enumerate(raw_images):
                    if not isinstance(item, dict):
                        raise ValueError(f"invalid_item:{index}")
                    data = base64.b64decode(
                        str(item.get("image_base64") or ""), validate=True
                    )
                    if not data:
                        raise ValueError(f"empty_image:{index}")
                    if len(data) > max_bytes:
                        raise ValueError(f"image_too_large:{index}")
                    total_bytes += len(data)
                    if total_bytes > max_total:
                        raise ValueError("batch_too_large")
                    mime = _pero_media_mime(data)
                    if mime is None:
                        raise ValueError(f"unsupported_image_type:{index}")
                    suffix = {
                        "image/png": ".png",
                        "image/jpeg": ".jpg",
                        "image/gif": ".gif",
                    }[mime]
                    path = media_dir / (
                        f"pero-b-{time.time_ns()}-{index}-"
                        f"{hashlib.sha256(data).hexdigest()[:12]}{suffix}"
                    )
                    path.write_bytes(data)
                    tmp_paths.append(path)
            except ValueError as exc:
                for path in tmp_paths:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                code = str(exc)
                return _batch_reply(
                    "unknown", ["skipped"] * len(raw_images),
                    code, code.split(":", 1)[0],
                    [code] * len(raw_images),
                )
            # 语义：**开始尝试但未拿到确认 → unknown**，绝不再用 skipped 冒充。
            # 已确认的项保留原状态；异常时当前项 unknown、后续未尝试项 skipped。
            text_state = "unknown"
            states = ["skipped"] * len(raw_images)
            errors = [""] * len(raw_images)
            evidence = ""
            try:
                names = [] if name.startswith("private:") else await asyncio.to_thread(wx.member_names, target_wxid)
                mention_parts = parse_mentions(body, names, max_mentions)
                if any(p["type"] == "at" for p in mention_parts) and any(
                    p["type"] == "text" and p["text"].strip() for p in mention_parts
                ):
                    text_outcome = await asyncio.to_thread(
                        wx.send_at_multi, name, mention_parts
                    )
                else:
                    text_outcome = await asyncio.to_thread(wx.send_text, name, body)
                if isinstance(text_outcome, SendResult):
                    text_state = text_outcome.state
                    evidence = text_outcome.detail or ""
                else:
                    text_state = "unknown"
                    evidence = "text_no_result"
                for index, path in enumerate(tmp_paths):
                    if text_state != "sent":
                        break
                    states[index] = "unknown"
                    try:
                        image_outcome = await asyncio.to_thread(
                            lambda p=path: wx.send_image(name, str(p), probe=False)
                        )
                    except Exception as exc:  # noqa: BLE001 - 当前张已开始操作 GUI
                        errors[index] = f"exception:{type(exc).__name__}"
                        evidence = (
                            f"{evidence} / image[{index}]:{errors[index]}"
                        ).strip(" /")
                        break
                    if isinstance(image_outcome, SendResult):
                        states[index] = image_outcome.state
                        if image_outcome.state != "sent":
                            errors[index] = image_outcome.detail or image_outcome.state
                        evidence = f"{evidence} / {image_outcome.detail or ''}".strip(
                            " /"
                        )
                    else:
                        states[index] = "unknown"
                        errors[index] = "image_no_result"
                    if states[index] != "sent":
                        # 不确定/失败即停止，后续不连发。
                        break
            except Exception as exc:  # noqa: BLE001
                evidence = f"exception:{type(exc).__name__}"
                if text_state == "sent":
                    # 文字已确认，异常发生在后续阶段：第一个未确认项记 unknown。
                    for index in range(len(states)):
                        if states[index] == "skipped":
                            states[index] = "unknown"
                            errors[index] = f"exception:{type(exc).__name__}"
                            break
            finally:
                for path in tmp_paths:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            return _batch_reply(text_state, states, evidence, errors=errors)
        if action == "pero_media_send_v1":
            # Pero 专用（P12）：文字+单图一次投递，默认休眠——没有入站 action
            # 就不会执行任何 GUI 操作。与旧 text_sticker 完全独立：
            # - 图片来自 action 内联 Base64（Pero 表情资产原件），不查 STICKERS 库；
            # - 禁止磁盘路径参数，只收 Base64 + 元数据；
            # - 文字先确认（SendResult.state），sent 才继续发图（probe=False）；
            #   failed/unknown 一律不继续、不重发；
            # - 桥不查询任何业务库、不决定发哪张图；只做目标解析、临时传输
            #   文件与 GUI 执行；只清理本次临时文件，不动源图或目录。
            gid = ("private:" + str(params.get("target_id", params.get("user_id")))) if params.get("conversation_type") == "private" or params.get("user_id") is not None else params.get("group_id")
            name, target_wxid = _target_of(params)
            if not name:
                return {"ok": False, "error": "group_not_found",
                        "text_state": "unknown", "image_state": "skipped"}
            body = str(params.get("text") or "")
            business_id = str(params.get("action_id") or "")[:80]
            if not business_id:
                return {"ok": False, "error": "missing_action_id",
                        "text_state": "unknown", "image_state": "skipped"}
            b64 = str(params.get("image_base64") or "")
            # 有界防重：同 (账号, 目标, 业务ID) 的重复动作不再执行 GUI，
            # 也不覆盖在途任务；防重表操作失败时不继续发送。
            dedup_key = (str(gid), name, business_id)
            if dedup_key in _PERO_MEDIA_DEDUP:
                return {"ok": False, "error": "duplicate_action",
                        "text_state": "unknown", "image_state": "skipped",
                        "evidence": "重复业务动作，未再次执行"}
            if len(_PERO_MEDIA_DEDUP) >= _PERO_MEDIA_DEDUP_MAX:
                _PERO_MEDIA_DEDUP.popitem(last=False)
            _PERO_MEDIA_DEDUP[dedup_key] = time.time()
            if not body.strip() or not b64.strip():
                return {"ok": False, "error": "invalid_params",
                        "text_state": "unknown", "image_state": "skipped"}
            try:
                data = base64.b64decode(b64, validate=True)
            except Exception:
                return {"ok": False, "error": "invalid_base64",
                        "text_state": "unknown", "image_state": "skipped"}
            mime = _pero_media_mime(data)
            if mime is None:
                return {"ok": False, "error": "unsupported_image_type",
                        "text_state": "unknown", "image_state": "skipped"}
            try:
                max_bytes = int(
                    (cfg.get("pero_media") or {}).get("max_image_bytes", 10_000_000)
                    or 10_000_000
                )
            except (TypeError, ValueError):
                max_bytes = 10_000_000
            if len(data) > max_bytes:
                return {"ok": False, "error": "image_too_large",
                        "text_state": "unknown", "image_state": "skipped"}
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif"}.get(
                mime, ".bin"
            )
            media_dir = Path(__file__).resolve().parent / "cache" / "pero_media"
            media_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = media_dir / (
                f"pero-{time.time_ns()}-{hashlib.sha256(data).hexdigest()[:12]}{suffix}"
            )
            tmp_path.write_bytes(data)
            text_state = "unknown"
            image_state = "unknown"
            evidence = ""
            try:
                names = [] if name.startswith("private:") else await asyncio.to_thread(wx.member_names, target_wxid)
                mention_parts = parse_mentions(body, names, max_mentions)
                if any(p['type']=='at' for p in mention_parts) and any(p['type']=='text' and p['text'].strip() for p in mention_parts):
                    text_outcome = await asyncio.to_thread(wx.send_at_multi, name, mention_parts)
                else:
                    text_outcome = await asyncio.to_thread(wx.send_text, name, body)
                if isinstance(text_outcome, SendResult):
                    # 明确按 state 判定：sent 才算确认；unknown 绝不当 sent（不用 bool()）。
                    text_state = text_outcome.state
                    evidence = text_outcome.detail or ""
                else:
                    text_state = "unknown"
                if text_state == "sent":
                    image_outcome = await asyncio.to_thread(
                        lambda: wx.send_image(name, str(tmp_path), probe=False)
                    )
                    if isinstance(image_outcome, SendResult):
                        image_state = image_outcome.state
                        evidence = f"{evidence} / {image_outcome.detail or ''}".strip(" /")
                    else:
                        image_state = "unknown"
                else:
                    # 文字未确认：图片不自行继续，也不自动重试。
                    image_state = "skipped"
            except Exception as exc:
                evidence = f"exception:{type(exc).__name__}"
                if text_state != "sent":
                    image_state = "skipped"
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            ok = text_state == "sent" and image_state == "sent"
            return {
                "ok": ok,
                "error": "" if ok else f"文字{text_state}/图片{image_state}",
                "text_state": text_state,
                "image_state": image_state,
                "text_message_id": "",
                "image_message_id": "",
                "evidence": evidence[:200],
            }
        if action == "pero_file_chunk_v1":
            # Pero 专用（P13）：**只读分块**文件查询，大文件端到端可传。
            # - 固定参数白名单：group_id + file_id + offset + length；
            # - length=0 只返回元数据（size/sha256/eof），不返回内容；
            # - 单块上限配置化（默认 1 MiB），与文件总大小限制分开；
            # - 不做正文解析、不调模型、不写业务库、不动源文件。
            gid = ("private:" + str(params.get("target_id", params.get("user_id")))) if params.get("conversation_type") == "private" or params.get("user_id") is not None else params.get("group_id")
            name, target_wxid = _target_of(params)
            if not name:
                return {"ok": False, "error": "group_not_found"}
            group_wxid = target_wxid
            fid = str(params.get("file_id") or "").strip()
            if not re.fullmatch(r"f_\d{4,8}", fid):
                return {"ok": False, "error": "invalid_file_id"}
            offset = params.get("offset")
            length = params.get("length")
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                return {"ok": False, "error": "invalid_offset"}
            if isinstance(length, bool) or not isinstance(length, int) or length < 0:
                return {"ok": False, "error": "invalid_length"}
            try:
                item = FILE_LIBRARY.get(fid, group_wxid)
            except Exception:
                return {"ok": False, "error": "file_not_found"}
            if not item:
                return {"ok": False, "error": "file_not_found"}
            fpath = (FILE_LIBRARY.root / item["file"]).resolve()
            try:
                st_size = fpath.stat().st_size
            except OSError:
                return {"ok": False, "error": "file_unreadable"}
            idx_size = int(item.get("size") or 0)
            if st_size <= 0:
                return {"ok": False, "error": "file_unreadable"}
            if idx_size and st_size != idx_size:
                return {"ok": False, "error": "size_mismatch"}
            if offset > st_size:
                return {"ok": False, "error": "offset_out_of_range"}
            try:
                chunk_limit = int(
                    (cfg.get("pero_file") or {}).get("max_chunk_bytes", 1_048_576)
                    or 1_048_576
                )
            except (TypeError, ValueError):
                chunk_limit = 1_048_576
            chunk_limit = max(65_536, min(chunk_limit, 8_388_608))
            if length > chunk_limit:
                return {"ok": False, "error": "chunk_too_large"}
            idx_sha = str(item.get("sha256") or "").strip().lower()
            common = {
                "file_id": fid,
                "name": str(item.get("name") or "")[:200],
                "size": st_size,
                "group_wxid": str(group_wxid or "")[:64],
                "sender_name": str(item.get("sender") or "")[:60],
                "source_local_id": str(item.get("source_local_id") or "")[:64],
                "offset": offset,
            }
            if length == 0:
                # 元数据：分块算整体 SHA256，避免整文件读进内存。
                digest = hashlib.sha256()
                try:
                    with fpath.open("rb") as f:
                        while True:
                            block = f.read(1024 * 1024)
                            if not block:
                                break
                            digest.update(block)
                except OSError:
                    return {"ok": False, "error": "file_unreadable"}
                actual_sha = digest.hexdigest()
                if idx_sha and actual_sha != idx_sha:
                    return {"ok": False, "error": "hash_mismatch"}
                return {
                    "ok": True,
                    "data": {
                        **common,
                        "sha256": actual_sha,
                        "length": 0,
                        "eof": offset >= st_size,
                        "content_base64": "",
                    },
                }
            read_len = min(length, st_size - offset)
            try:
                with fpath.open("rb") as f:
                    f.seek(offset)
                    data = f.read(read_len)
            except OSError:
                return {"ok": False, "error": "file_unreadable"}
            if len(data) != read_len:
                return {"ok": False, "error": "short_read"}
            return {
                "ok": True,
                "data": {
                    **common,
                    "sha256": "",
                    "length": len(data),
                    "eof": offset + len(data) >= st_size,
                    "content_base64": base64.b64encode(data).decode("ascii"),
                },
            }
        if action == "pero_file_bytes_v1":
            # Pero 专用（P13）：**只读**文件原件查询。与发送回执语义区分：
            # status=ok 只代表查询成功，不代表任何消息 sent。
            # - 固定参数白名单：group_id + file_id；不接受路径/URL/SQL；
            # - 先验证句柄属于目标群（FileLibrary.get 内置群归属+路径逃逸校验），
            #   再按入库时记录的大小/类型读取（≤入库上限 5MB）并编码；
            # - 不做正文解析、不调 Files/模型、不搬 Agent 逻辑入桥；
            # - 默认休眠：没有入站 action 就不执行任何读取。
            gid = ("private:" + str(params.get("target_id", params.get("user_id")))) if params.get("conversation_type") == "private" or params.get("user_id") is not None else params.get("group_id")
            name, target_wxid = _target_of(params)
            if not name:
                return {"ok": False, "error": "group_not_found"}
            group_wxid = target_wxid
            fid = str(params.get("file_id") or "").strip()
            if not re.fullmatch(r"f_\d{4,8}", fid):
                return {"ok": False, "error": "invalid_file_id"}
            try:
                item = FILE_LIBRARY.get(fid, group_wxid)
            except Exception:
                return {"ok": False, "error": "file_not_found"}
            if not item:
                return {"ok": False, "error": "file_not_found"}
            fpath = (FILE_LIBRARY.root / item["file"]).resolve()
            # P13-R01：stat + read(max+1) 硬上限；磁盘原件与入库索引版本
            # 不一致（可得版本校验）直接拒绝，不再先 read_bytes 全量分配。
            try:
                st_size = fpath.stat().st_size
            except OSError:
                return {"ok": False, "error": "file_unreadable"}
            idx_size = int(item.get("size") or 0)
            if st_size <= 0:
                return {"ok": False, "error": "file_unreadable"}
            if st_size > _PERO_QUERY_MAX_BYTES:
                return {"ok": False, "error": "file_too_large"}
            if idx_size and st_size != idx_size:
                return {"ok": False, "error": "size_mismatch"}
            try:
                with fpath.open("rb") as f:
                    data = f.read(_PERO_QUERY_MAX_BYTES + 1)
            except OSError:
                return {"ok": False, "error": "file_unreadable"}
            if len(data) > _PERO_QUERY_MAX_BYTES or len(data) != st_size:
                return {"ok": False, "error": "size_mismatch"}
            actual_sha = hashlib.sha256(data).hexdigest()
            idx_sha = str(item.get("sha256") or "").strip().lower()
            if idx_sha and actual_sha != idx_sha:
                return {"ok": False, "error": "hash_mismatch"}
            return {
                "ok": True,
                "data": {
                    "file_id": fid,
                    "name": str(item.get("name") or "")[:200],
                    "size": len(data),
                    "sha256": actual_sha,
                    "group_wxid": str(group_wxid or "")[:64],
                    "sender_name": str(item.get("sender") or "")[:60],
                    "source_local_id": str(item.get("source_local_id") or "")[:64],
                    "content_base64": base64.b64encode(data).decode("ascii"),
                },
            }
        if action == "pero_quote_image_v1":
            # P13-R01：Pero 专用**只读**引用原图查询。参数白名单：
            # group_id + image_md5（32 位 hex）+ 可选 refer_time；
            # 查找链限定当前群（_pero_resolve_quote_image），找不到一律
            # image_not_found，不拿其他图顶替；只读不写缓存、不发消息。
            _, wxid = _target_of(params)
            if not wxid:
                return {"ok": False, "error": "group_not_found"}
            qmd5 = str(params.get("image_md5") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{32}", qmd5):
                return {"ok": False, "error": "invalid_image_md5"}
            qtime = str(params.get("refer_time") or "").strip()[:20]
            qpath, qsource = await asyncio.to_thread(
                _pero_resolve_quote_image, wx, wxid, qmd5, qtime
            )
            if not qpath:
                return {"ok": False, "error": "image_not_found"}
            try:
                st_size = Path(qpath).stat().st_size
            except OSError:
                return {"ok": False, "error": "image_unreadable"}
            if st_size <= 0 or st_size > _PERO_QUERY_MAX_BYTES:
                return {"ok": False, "error": "image_too_large"}
            try:
                with Path(qpath).open("rb") as f:
                    qdata = f.read(_PERO_QUERY_MAX_BYTES + 1)
            except OSError:
                return {"ok": False, "error": "image_unreadable"}
            if len(qdata) > _PERO_QUERY_MAX_BYTES or len(qdata) != st_size:
                return {"ok": False, "error": "image_unreadable"}
            return {
                "ok": True,
                "data": {
                    "image_md5": qmd5,
                    "group_wxid": str(wxid or "")[:64],
                    "source": qsource[:120],
                    "size": len(qdata),
                    "sha256": hashlib.sha256(qdata).hexdigest(),
                    "content_base64": base64.b64encode(qdata).decode("ascii"),
                },
            }
        if action == "list_files":
            gid = params.get("group_id")
            wxid = _wxid_of(gid)
            if not wxid:
                return {"ok": False, "error": "group_not_found"}
            try:
                limit = int(params.get("limit") or 20)
            except (TypeError, ValueError):
                limit = 20
            return {
                "ok": True,
                "items": FILE_LIBRARY.list(
                    wxid,
                    query=str(params.get("query") or ""),
                    ext=str(params.get("ext") or ""),
                    limit=limit,
                ),
            }
        if action == "read_file":
            gid = params.get("group_id")
            wxid = _wxid_of(gid)
            if not wxid:
                return {"ok": False, "error": "group_not_found"}
            item = FILE_LIBRARY.get(
                str(params.get("file_id") or "").strip(), wxid
            )
            if not item:
                return {"ok": False, "error": "file_not_found"}
            try:
                payload = Path(item["path"]).read_bytes()
            except OSError as e:
                return {"ok": False, "error": f"read_failed: {e}"}
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    text = payload.decode("gb18030")
                except UnicodeDecodeError:
                    text = payload.decode("utf-8", errors="replace")
            try:
                max_chars = max(1, min(int(params.get("max_chars") or 12000), 20000))
            except (TypeError, ValueError):
                max_chars = 12000
            truncated = len(text) > max_chars
            return {
                "ok": True,
                "id": item["id"],
                "name": item["name"],
                "size": item["size"],
                "sha256": hashlib.sha256(payload).hexdigest(),
                "read_chars": min(len(text),max_chars),
                "total_chars": len(text),
                "content": text[:max_chars],
                "truncated": truncated,
            }
        if action == "can_send_image":
            return {"yes": True}
        if action == "can_send_record":
            return {"yes": False}
        if action == "send_private_msg" or (action == "send_msg" and
                (params.get("message_type") == "private" or params.get("user_id") is not None)):
            target = dict(params, conversation_type="private")
            name, _ = _target_of(target)
            text = clean_markdown(_extract_text(params.get("message")))
            if not name or not text:
                raise ActionFailed("private_target_not_authorized_or_empty_message")
            outcome = await asyncio.to_thread(wx.send_text, name, text)
            if not outcome:
                raise ActionFailed(outcome.detail, delivery_state=outcome.state,
                                   retcode=1201 if outcome.state == "unknown" else 1200)
            return {"message_id": bot.next_msg_id()}
        if action in ("send_group_msg", "send_msg"):
            gid = params.get("group_id")
            name = _name_of(gid)
            raw_msg = params.get("message")
            text = clean_markdown(_extract_text(raw_msg))
            if not name or not text:
                # 不再静默丢弃：之前「合并转发」段就是这样无声消失的
                #（模型生成完、AstrBot 也 Prepare to send，桥侧毫无痕迹）。
                seg_types = (
                    [s.get("type") for s in raw_msg if isinstance(s, dict)]
                    if isinstance(raw_msg, list)
                    else [type(raw_msg).__name__]
                )
                log.warning(
                    "[发送] 丢弃了一条转不出文字的消息（群=%s，段类型=%s）",
                    name or "?",
                    seg_types,
                )
                raise ActionFailed("没有可发送的文字，或目标群不存在")
            # 模型写了 @某人 → 换成微信**真实 @**（对方会收到提醒）。
            # 支持一条回复里 @ 多个人：桥按正文顺序「粘一段 → 插一个 @ → 再粘」。
            # 只有名字确实是本群成员时才走 @，否则按普通文本发。
            parts = parse_mentions(
                text, wx.member_names(_wxid_of(gid)), max_mentions
            )
            at_names = [p["name"] for p in parts if p["type"] == "at"]
            has_text = any(
                p["type"] == "text" and p["text"].strip() for p in parts
            )
            if at_names and has_text:
                log.info(
                    "[发送@] %s -> %s（%d 个）: %s",
                    name,
                    "、".join(at_names),
                    len(at_names),
                    text[:40],
                )
                ok = await asyncio.to_thread(
                    wx.send_at_multi, name, parts
                )
            else:
                ok = await asyncio.to_thread(wx.send_text, name, text)
            if not ok:
                state = ok.state if isinstance(ok, SendResult) else "unknown"
                detail = ok.detail if isinstance(ok, SendResult) else "微信未确认发送结果"
                raise ActionFailed(detail, delivery_state=state,
                                   retcode=1201 if state == "unknown" else 1200)
            return {"message_id": bot.next_msg_id()}
        # 兜底：AstrBot 的长回复会走 OneBot 的**合并转发**动作
        # （send_group_forward_msg），微信这边没法用 UI 自动化做真·合并转发
        # （要选中多条消息再转发，太脆），所以把 nodes 里的文本抽出来当普通
        # 长消息发 —— 内容完整，只是没有「聊天记录」卡片样式。
        # 主配置里已把 forward_threshold 调大（1500 → 999999），正常不会再走到
        # 这条路；留着它是因为「无声消失」的代价太大（踩过一次）。
        if action == "send_group_forward_msg":
            gid = params.get("group_id")
            name = _name_of(gid)
            text = clean_markdown(_extract_text(params.get("messages") or []))
            if not name or not text:
                log.warning(
                    "[发送] 合并转发里转不出文字（群=%s，参数键=%s）",
                    name or "?",
                    list(params.keys()),
                )
                raise ActionFailed("没有可发送的文字，或目标群不存在")
            log.info("[发送] 合并转发 → 按普通长消息发出（%d 字）", len(text))
            ok = await asyncio.to_thread(wx.send_text, name, text)
            if not ok:
                state = ok.state if isinstance(ok, SendResult) else "unknown"
                detail = ok.detail if isinstance(ok, SendResult) else "微信未确认发送结果"
                raise ActionFailed(detail, delivery_state=state,
                                   retcode=1201 if state == "unknown" else 1200)
            return {"message_id": bot.next_msg_id()}

        # 未实现的动作：**发送类**必须告警（静默失败极难排查），查询类记 debug
        if action.startswith("send_"):
            log.warning(
                "[动作] 未实现的发送动作：%s（参数键=%s）——这条消息不会发出去",
                action,
                list(params.keys())[:8],
            )
            raise ActionFailed(f"未实现的发送动作：{action}", retcode=1404)
        else:
            log.debug("未处理的 action: %s", action)
        return None

    async def handle_action(action: str, params: dict):
        """GUI 动作统一排队；同一时刻只有一个动作在操作微信窗口。"""

        if action in gui_actions:
            async with gui_lock:
                return await _dispatch(action, params)
        return await _dispatch(action, params)

    def _private_wxid_of(user_id) -> str:
        item = authorized_privates.get(str(user_id))
        if not item:
            return ""
        wxid = str(item.get("user_wxid") or "")
        return wxid if str(private_user_id(wxid)) == str(user_id) else ""

    def _target_of(params: dict) -> tuple[str, str]:
        kind = params.get("conversation_type") or ("private" if params.get("user_id") is not None else "group")
        if kind == "private":
            target = params.get("target_id", params.get("user_id"))
            wxid = _private_wxid_of(target)
            return ("private:" + wxid, wxid) if wxid else ("", "")
        if kind != "group":
            return "", ""
        target = params.get("target_id", params.get("group_id"))
        return _name_of(target), _wxid_of(target)

    def _name_of(group_id) -> str:
        for g in groups:
            if str(_fake_id(g["id"])) == str(group_id) or g["id"] == str(group_id):
                return g["name"]
        return ""

    def _wxid_of(group_id) -> str:
        """OneBot 群号 → 微信 wxid（取群成员名单必须用 wxid）。"""
        for g in groups:
            if str(_fake_id(g["id"])) == str(group_id) or g["id"] == str(group_id):
                return g["id"]
        return ""

    def _extract_text(message) -> str:
        """从 OneBot 消息段里抽纯文本。

        **必须穿透「合并转发」（node / nodes）**：AstrBot 会把超过
        `platform_settings.forward_threshold`（默认 1500 字）的回复自动打包成
        合并转发 —— 而这里以前只认 `text` 段，长回复（比如 KMP 那种整段代码
        答案）就被**静默丢弃**，群里什么都收不到（2026-10-08 实况：模型明明
        生成完了、AstrBot 也「Prepare to send」了，桥侧一条日志都没有）。
        现在把转发里的文本递归抽出来，按普通文本发出。
        """
        if isinstance(message, str):
            return message
        if isinstance(message, dict):
            return _extract_text([message])
        if not isinstance(message, list):
            return ""
        parts: list[str] = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "text":
                parts.append(str(data.get("text") or ""))
            elif stype == "node":
                inner = _extract_text(data.get("content") or [])
                if inner:
                    parts.append(inner + "\n")
            elif stype == "nodes":
                inner = _extract_text(
                    data.get("messages") or data.get("nodes") or data.get("content") or []
                )
                if inner:
                    parts.append(inner + "\n")
        return "".join(parts)

    bot = OneBotClient(
        ws_url=cfg["astrbot"]["ws_url"],
        self_id=cfg["astrbot"].get("self_id", 10001),
        token=(os.environ.get(str(cfg["astrbot"].get("token_env") or ''), '').strip()
               if cfg["astrbot"].get("token_env") else cfg["astrbot"].get("token", "")),
        heartbeat=float(cfg["astrbot"].get("heartbeat_seconds", 15)),
        action_handler=handle_action,
        identity_directory=identities,
    )
    asyncio.create_task(bot.run())

    # ── 统一增量读取 + 分发上报（B02/B03/B04）────────────
    # 读取层与分发层分离：读取只做数据库增量查询（小批、按会话公平轮转），
    # 媒体解密/文件入库/OneBot 上报在每会话独立的串行队列里做 ——
    # 一个会话在解密图片时不会挡住其它会话（尤其私聊）的读取。
    interval = float(cfg["wechat"].get("poll_interval", 1.5))
    send_images = bool(cfg["wechat"].get("send_images", True))
    image_max_bytes = int(cfg["wechat"].get("image_max_bytes", 10_000_000))
    # 先发图、再问图：按「群 + 发送者」记住最近一张图的消息段（含 base64）。
    image_carry_seconds = float(cfg["wechat"].get("image_carry_seconds", 30.0) or 0)
    last_images: dict[tuple[str, str], tuple[float, list[dict]]] = {}
    read_batch = max(1, int(cfg["wechat"].get("read_batch", 50) or 50))
    dispatch_queue_max = max(
        1, int(cfg["wechat"].get("dispatch_queue_max", 200) or 200)
    )
    metrics_log_seconds = float(cfg["wechat"].get("metrics_log_seconds", 30.0) or 0)
    dispatch_max_attempts = max(1, int(cfg["wechat"].get("dispatch_max_attempts", 3) or 3))
    dispatch_retry_base = float(cfg["wechat"].get("dispatch_retry_base", 0.5) or 0.5)
    dispatch_retry_max = float(cfg["wechat"].get("dispatch_retry_max", 10.0) or 10.0)
    private_sync_seconds = max(0.5, float(private_cfg.get("sync_seconds", 5.0) or 5.0))
    private_discover_seconds = float(private_cfg.get("discover_seconds", 15.0) or 0)

    # Existing history is never replayed into redemption or model processing.
    try:
        initial_privates = await asyncio.to_thread(wx.private_sessions)
    except Exception as exc:
        log.warning("Private startup watermark failed (%s)", type(exc).__name__)
        initial_privates = []
    private_started_at = int(time.time())
    # 启动阶段快照水位：授权会话从这里开始读，启动后到达的消息一条都不越过；
    # 比 `private_started_at` 更早的旧聊天仍由时间闸门挡住，不回放。
    # 严格读取 + 逐会话处理：查不到的会话**不记 0**（否则等于从头读），
    # 也不因为一个会话异常中止全部快照。
    private_startup_seq: dict[str, int] = {}
    try:
        private_startup_seq = await asyncio.to_thread(
            collect_startup_seq,
            initial_privates,
            lambda peer: wx.latest_seq(peer, strict=True),
        )
    except Exception as exc:  # noqa: BLE001 - 快照整体失败：按"无快照"继续
        log.warning(
            "[私聊] 启动快照整体失败（%s）：本次不记快照", type(exc).__name__
        )
        private_startup_seq = {}

    # 未授权会话只推进水位、不落正文；开通成功后按水位基线开始常规增量读取。
    # （开通码水位/撤销水位由 PrivateGate 持有，见下方 `gate`。）
    private_discovery_seq: dict[str, int] = dict(private_startup_seq)
    private_attempts = PrivateAttempts()

    def _event_create_time(m: dict) -> int:
        try:
            return int(float((m.get("raw") or {}).get("create_time") or 0))
        except (TypeError, ValueError, AttributeError):
            return 0

    def _should_dispatch(target, m: dict) -> bool:
        """自身回声/启动前的历史事件：只推进水位，不分发（不等于已上报）。"""
        return default_event_filter(target.kind, m, private_started_at)

    def _stable_mid(m: dict) -> int:
        """同一条事件重试时必须复用同一个 `message_id`。

        Pero 侧入站幂等按 source/event ID 判定，重分配 ID 会被当成新消息重复回答；
        首次分配后挂在事件字典上，重试路径原样取回。
        """
        mid = m.get("_bridge_mid")
        if isinstance(mid, int) and not isinstance(mid, bool):
            return mid
        mid = bot.next_msg_id()
        m["_bridge_mid"] = mid
        return mid

    def _identity_for(m: dict, gid: str) -> dict:
        """身份扩展里不携带调度内部字段（`_` 前缀只用于本地重试）。"""
        ident = {k: v for k, v in m.items() if not str(k).startswith("_")}
        ident["group_wxid"] = gid
        return ident

    async def _dispatch_group(target, m: dict) -> None:
        """群聊：准备一次（媒体段 / 扩展 / 身份 / 事件 ID），上报可安全重试。

        * 准备阶段只执行一次：回声判定、图片编号与入库、图片跟随的一次性 `pop`
          都记在事件的 `_prepared` 里；上报失败重试时直接复用同一份 segments、
          扩展与 `message_id` —— 不重新消费一次性引用、不丢已挂上的图片、
          事件身份稳定（Pero 入站幂等成立）。
        * 媒体处理**异常向上抛**给 `UnifiedPoller._deliver()`（按非传输错误重试 /
          计数），不再"打日志后 return"被当成已分发。明确跳过的情况（未开图片
          通道、自身消息、回声、空文本 / XML、无消息段）仍然只是 return。
        """
        gid, gname = target.ref, target.name
        mtype = str(m.get("type") or "")

        prepared = m.get("_prepared")
        if not isinstance(prepared, dict):
            prepared = None

        if prepared is None:
            # ── 非文本：图片 / 表情包 / 语音 / 链接 ────────────────
            # 图片要先解密落到本地再读成 base64，是磁盘 IO —— 放线程里做，
            # 而且这里已经是该会话的独立分发任务，不会拖慢其它会话的读取。
            if "文本" not in mtype:
                if not send_images:
                    return
                if m.get("is_self"):
                    log.info("[跳过] 自己发送的非文本消息（类型=%s）", mtype)
                    return
                if ("图片" in mtype or "表情" in mtype) and wx.consume_self_image_echo(
                    gname, m.get("sender_name", "")
                ):
                    log.info("[跳过] 机器人自己发图的回声（%s）", gname)
                    return
                ext_sink: dict = {}
                try:
                    segments = await asyncio.to_thread(
                        build_media_segments,
                        m,
                        gid,
                        wx,
                        image_max_bytes,
                        mention_aliases,
                        cfg["astrbot"].get("self_id", 10001),
                        ext_sink,
                    )
                except Exception as e:  # noqa: BLE001
                    # 交给调度器：事件保留并按策略重试 / 计数，绝不记成已分发。
                    log.warning(
                        "处理非文本消息失败（%s / 类型=%s）: %s", gname, mtype, e
                    )
                    raise
                if not segments:
                    log.debug("[跳过] %s 类型=%s", gname, mtype)
                    return

                img_seg = next((s for s in segments if s.get("type") == "image"), None)
                if img_seg is not None and not m.get("is_self"):
                    # 纯图片事件在 AstrBot 侧不一定能进插件缓存，桥这里先留一份，
                    # 等同一人 30 秒内发「这是什么」时直接并进那条追问。
                    last_images[(gid, m["sender_wxid"] or m["sender_name"])] = (
                        time.time(),
                        segments,
                    )
                    if image_carry_seconds > 0:
                        now_ts = time.time()
                        for k in [
                            k
                            for k, (ts, _) in last_images.items()
                            if now_ts - ts > image_carry_seconds
                        ]:
                            last_images.pop(k, None)
                log.info(
                    "[收到] %s / %s: <%s>",
                    gname,
                    m["sender_name"],
                    "图片" if img_seg else mtype,
                )
                log.info(
                    "[上报] group=%s user=%s segments=%s",
                    gname,
                    m["sender_name"],
                    (
                        f"<image base64 {len(img_seg['data']['file'])} 字节>"
                        if img_seg
                        else json.dumps(segments, ensure_ascii=False)
                    ),
                )

                # P13：版本化身份/引用/文件扩展（旧标准字段原样保留）。
                pero_ext = _pero_identity_ext(m, {"id": gid})
                quote_ext = _pero_quote_ext(m.get("text") or "")
                if quote_ext:
                    pero_ext["quote"] = quote_ext
                if isinstance(ext_sink.get("file"), dict):
                    pero_ext["file"] = ext_sink["file"]
            else:
                if not m["text"]:
                    return
                if m["text"].lstrip().startswith("<"):
                    log.debug("[跳过] XML 内容: %s", m["text"][:60])
                    return

                log.info("[收到] %s / %s: %s", gname, m["sender_name"], m["text"][:60])

                segments = build_segments(
                    m["text"],
                    mention_aliases,
                    cfg["astrbot"].get("self_id", 10001),
                )
                if not segments:
                    return

                # 「先发图、再问图」兜底：同一人刚才发过图，本条又在问图 →
                # 把图补进这条消息。AstrBot 的纯图片事件不一定触发插件缓存，
                # 这里让图片和追问走同一条事件，模型就不会再说「没收到图片」。
                # 注意 `last_images.pop` 是一次性消费：只在这里执行一次，
                # 结果随 `_prepared` 缓存，重试不会丢图也不会二次消费。
                bare_at = any(s.get("type") == "at" for s in segments) and not any(
                    s.get("type") == "text"
                    and str(s.get("data", {}).get("text", "")).strip()
                    for s in segments
                )
                if (
                    image_carry_seconds > 0
                    and (IMAGE_QUERY.search(m["text"] or "") or bare_at)
                    and not any(s.get("type") == "image" for s in segments)
                ):
                    key = (gid, m["sender_wxid"] or m["sender_name"])
                    hit = last_images.pop(key, None)
                    if hit and time.time() - hit[0] <= image_carry_seconds:
                        segments = segments + hit[1]
                        log.info(
                            "[图片跟随] %s / %s：已把上一条的图补挂到本条",
                            gname,
                            m["sender_name"],
                        )

                log.info(
                    "[上报] group=%s user=%s segments=%s",
                    gname,
                    m["sender_name"],
                    json.dumps(segments, ensure_ascii=False),
                )
                pero_ext = _pero_identity_ext(m, {"id": gid})

            # 准备结果缓存：重试复用，不再碰一次性状态。
            prepared = {
                "group_id": _fake_id(gid),
                "segments": segments,
                "user_id": _fake_id(m["sender_wxid"] or m["sender_name"]),
                "nickname": m["sender_name"],
                "identity": _identity_for(m, gid),
                "pero_ext": pero_ext,
                "message_id": _stable_mid(m),
            }
            m["_prepared"] = prepared

        await bot.send_group_message(**prepared)

    async def _dispatch_private(target, m: dict) -> None:
        """已授权私聊：正常消息都上报；类型不被底层支持时给真实类型占位。

        与群聊一致：授权校验**每次重试都做**（撤销后立刻停投），准备结果
        （媒体段 / 扩展 / `message_id`）缓存到 `_prepared`，重试不重复下载、
        不重复入库，事件身份稳定。
        """
        wxid = target.ref
        ref = str(private_user_id(wxid))
        grant = authorized_privates.get(ref)
        if not grant or grant.get("user_wxid") != wxid:
            return  # No files, logs, history, hints or media work for unknown users.
        mtype = str(m.get("type") or "")
        content = str(m.get("text") or "").strip()
        is_text = "文本" in mtype
        # Codes never become model input, even if this user already has a grant.
        if is_text and is_activation_code_format(content):
            return

        prepared = m.get("_prepared")
        if not isinstance(prepared, dict):
            prepared = None

        if prepared is None:
            ext = {
                "v": 1,
                "conversation_type": "private",
                "account_id": private_account_id,
                "sender_wxid": wxid,
                "user_wxid": wxid,
                "bridge_user_id": int(ref),
                "local_id": str(m.get("local_id") or ""),
                "source_event_id": (
                    f"{wx.self_wxid}:{wxid}:{m.get('seq')}:{m.get('local_id')}"
                ),
                "conversation_id": grant.get("conversation_id"),
                "status_revision": grant.get("status_revision"),
                "sources": {"sender_wxid": "message_db_sender_username"},
            }
            ext_sink: dict = {}
            if is_text:
                if not content or content.startswith("<"):
                    return
                segments = [{"type": "text", "data": {"text": m["text"]}}]
            else:
                segments = []
                if send_images:
                    # 媒体失败向上抛（调度器重试/计数），不是"跳过"。
                    segments = await asyncio.to_thread(
                        build_media_segments,
                        m,
                        wxid,
                        wx,
                        image_max_bytes,
                        [],
                        bot.self_id,
                        ext_sink,
                    ) or []
                    if isinstance(ext_sink.get("file"), dict):
                        ext["file"] = ext_sink["file"]
                if not segments:
                    # 底层确实拿不到原件（或图片通道关闭）：给**真实类型**占位，
                    # 让模型知道"这里有一条语音/视频…"，绝不因类型本身丢弃整条消息。
                    segments = [
                        {"type": "text", "data": {"text": f"[{mtype or '非文本'}消息]"}}
                    ]
            prepared = {
                "user_id": int(ref),
                "segments": segments,
                "nickname": str(m.get("sender_name") or ""),
                "pero_ext": ext,
                "message_id": _stable_mid(m),
            }
            m["_prepared"] = prepared

        await bot.send_private_message(**prepared)

    poller = UnifiedPoller(
        read=wx.read_incremental,
        dispatch=lambda target, event: (
            _dispatch_group(target, event)
            if target.kind == KIND_GROUP
            else _dispatch_private(target, event)
        ),
        filter_event=_should_dispatch,
        interval=interval,
        queue_max=dispatch_queue_max,
        read_batch=read_batch,
        metrics_log_seconds=metrics_log_seconds,
        max_attempts=dispatch_max_attempts,
        retry_base=dispatch_retry_base,
        retry_max=dispatch_retry_max,
    )
    for g in groups:
        if g["id"] not in last_seq:
            continue  # 启动水位未知：本进程不监听该群（避免从 0 回放历史）
        poller.add_target(KIND_GROUP, g["id"], g["name"], last_seq[g["id"]])

    async def _latest_seq(wxid: str) -> int:
        """授权门用的**严格**水位：数据库异常必须上抛（→ `WatermarkUnknown` → 暂缓）。"""

        return await asyncio.to_thread(wx.latest_seq, wxid, strict=True)

    # 私聊授权门：目标集合、读取起点、投递闸门都在这里（可离线验收）。
    gate = PrivateGate(
        poller,
        latest_seq=_latest_seq,
        startup_seq=private_startup_seq,
        account_id=private_account_id,
    )
    # 分发侧读同一个字典（原地更新），不需要 nonlocal 重新绑定。
    authorized_privates = gate.grants

    async def _private_sync_loop() -> None:
        """低频同步授权名单：不参与常规聊天读取，撤销后立即停止轮询该会话。"""
        while True:
            try:
                result = await sync_authorized_privates(
                    pero_url, internal_token, private_account_id
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 未知异常按"不可验证"处理
                result = SyncResult(False, {}, type(exc).__name__)
            if result.ok:
                # 先应用名单，成功后才解除暂停（绝不用旧名单放行）。
                # 成功且空名单 = 真的都撤销了：停止读取并丢弃待投递事件。
                await gate.apply(result.grants)
                gate.set_paused(False)
            else:
                # 网络失败/无令牌：不清队列、不改水位，等下一次成功同步。
                gate.set_paused(True, result.reason)
            await asyncio.sleep(private_sync_seconds)

    async def _private_discovery_loop() -> None:
        """未授权会话的独立低频发现 + 开通码监听（不读正文、不碰模型）。"""
        while True:
            await asyncio.sleep(private_discover_seconds)
            if not internal_token:
                continue
            try:
                sessions = await asyncio.to_thread(wx.private_sessions)
            except Exception as exc:  # noqa: BLE001
                log.warning("[私聊] 会话发现失败（%s）", type(exc).__name__)
                continue
            for session in sessions:
                wxid = str(session.get("username") or "")
                if not wxid or poller.has_target(KIND_PRIVATE, wxid):
                    continue  # 已授权会话走常规增量读取
                if wxid not in private_discovery_seq:
                    # 严格取起点：查不到就**本次跳过这个会话**（不写 0、不读旧消息），
                    # 下一轮发现再试；不做任何正文读写。
                    try:
                        seq = await asyncio.to_thread(wx.latest_seq, wxid, strict=True)
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "[私聊] 发现起点读取失败（%s）：本次跳过该会话",
                            type(exc).__name__,
                        )
                        continue
                    private_discovery_seq[wxid] = max(0, int(seq) - 1)
                try:
                    msgs = await asyncio.to_thread(
                        wx.new_private_messages, wxid, private_discovery_seq[wxid]
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("[私聊] 发现读取失败（%s）", type(exc).__name__)
                    continue
                for m in msgs:
                    seq = int(m.get("seq") or 0)
                    private_discovery_seq[wxid] = max(
                        private_discovery_seq[wxid], seq
                    )
                    if m.get("is_self"):
                        continue
                    if _event_create_time(m) < private_started_at:
                        continue
                    if "文本" not in str(m.get("type") or ""):
                        continue  # 未授权会话不收录任何媒体
                    text = str(m.get("text") or "").strip()
                    if not is_activation_code_format(text):
                        continue
                    ref = str(private_user_id(wxid))
                    if not private_attempts.allow(ref):
                        continue
                    result = await redeem_activation_code(
                        pero_url, internal_token, "onebot11", private_account_id,
                        ref, wxid, text,
                        f"{wx.self_wxid}:{wxid}:{seq}:{m.get('local_id')}",
                        str(m.get("sender_name") or ""),
                    )
                    if not result.get("success"):
                        continue
                    # 兑换成功：以**这条已确认处理的开通码事件**的 seq 作为常规
                    # 读取起点。之后到达的普通消息一条不跳过，开通码本身不再重读。
                    private_discovery_seq[wxid] = max(
                        private_discovery_seq[wxid], seq
                    )
                    gate.remember_redeem(wxid, seq)
                    sync = await sync_authorized_privates(
                        pero_url, internal_token, private_account_id
                    )
                    if sync.ok:
                        # 先应用（用开通码水位建目标），成功后再放行投递。
                        await gate.apply(sync.grants)
                        gate.set_paused(False)
                    else:
                        # 名单暂时不可验证：起点先留着，下一次成功同步再加目标。
                        gate.set_paused(True, sync.reason)
                    # Fixed receipt; errors never include server-provided text or code.
                    async with gui_lock:
                        await asyncio.to_thread(
                            wx.send_text,
                            "private:" + wxid,
                            "对话已开通。现在可以直接与我聊天。",
                        )

    # 启动对齐：先取一次授权名单，再建立私聊会话目标。
    # 成功 → 用启动快照水位；失败 → 暂停投递（下一个同步周期重试），不销毁任何状态。
    try:
        startup_sync = await sync_authorized_privates(
            pero_url, internal_token, private_account_id
        )
    except Exception as exc:  # noqa: BLE001
        startup_sync = SyncResult(False, {}, type(exc).__name__)
    if startup_sync.ok:
        await gate.apply(startup_sync.grants)
        gate.set_paused(False)
    else:
        gate.set_paused(True, startup_sync.reason)

    await poller.start()
    background_tasks = [asyncio.create_task(_private_sync_loop())]
    if private_discover_seconds > 0:
        background_tasks.append(asyncio.create_task(_private_discovery_loop()))
    log.info(
        "[读取] 统一轮询已启动：群 %d 个、私聊 %d 个、间隔 %.2fs",
        len(poller.targets(KIND_GROUP)),
        len(poller.targets(KIND_PRIVATE)),
        interval,
    )
    try:
        await asyncio.gather(*background_tasks)
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await poller.aclose()
        log.info("[读取] 读取/分发/授权任务已全部停止")

if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n已退出")
