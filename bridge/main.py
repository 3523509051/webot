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
import html
import json
import logging
import os
import re
import subprocess
import sys
from io import BytesIO
from pathlib import Path

from onebot11 import OneBotClient
from wechat_client import WeChatClient

CONFIG_PATH = Path(__file__).with_name("config.json")

# 模块级 logger：build_segments / image_segment / card_segments 这些都是**模块级函数**，
# 也要打日志。早先只在 main() 里定义了 log，结果这些函数一走到日志分支就
# NameError 崩掉（用真实引用回复消息做本地验证时才暴露出来）。
log = logging.getLogger("bridge")

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
        "image_max_bytes": 3000000,
        "image_max_edge": 1280,  # 限制像素边长，文件字节上限并不能控制视觉 token
        # 动画表情（表情包）当图片处理：从 XML 的 cdnurl 取原文件，动图只取第一帧
        "fetch_stickers": True,
    },
    # wechatauto_level=DEBUG 时会打出侧栏查找 / 兜底搜索 / open_chat 的细节，
    # 用于验证「已在目标群时不做切换」。零运行时开销（只是放行已有日志）。
    "logging": {"level": "INFO", "wechatauto_level": "DEBUG"},
}


# 微信 @ 之后会跟各种不可见空白（thin space / nbsp / 全角空格 等）
MENTION_SPACES = r"[\s\u2005\u00a0\u3000\u2002\u2003\u2009\u202f]*"


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
MEDIA_OPTS: dict = {"fetch_stickers": True, "image_max_edge": 1280}


def init_media_opts(cfg: dict) -> None:
    """把 main() 读到的配置同步给模块级媒体开关。"""
    wx_cfg = cfg.get("wechat") or {}
    MEDIA_OPTS["fetch_stickers"] = bool(wx_cfg.get("fetch_stickers", True))
    MEDIA_OPTS["image_max_edge"] = int(wx_cfg.get("image_max_edge", 1280))

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
    refer_md5 = None
    refer_content = ""
    if refer_body:
        mm = re.search(r'md5="([0-9a-fA-F]{32})"', refer_body)
        if mm:
            refer_md5 = mm.group(1).lower()
        elif not refer_body.lstrip().startswith("<"):
            # 纯文本引用：**把被引用的原话带出来**。
            # 为什么必须带：之前只带「（引用 X 的消息）」，模型看不到原文 ——
            # 实测对方引用一句旧话问「这句话」，机器人只能答「我只看到『这句话』
            # 三个字，原话没捞上来」（2026-10-08）。引用图片时这里是一段 XML，
            # 图片另有 md5 通道，跳过。
            refer_content = html.unescape(refer_body).strip()
    return {
        "kind": APPMSG_LABELS.get(_xml_tag(text, "type"), "卡片"),
        "title": _xml_tag(text, "title"),
        "des": _xml_tag(text, "des"),
        "refer_author": _xml_tag(refer, "displayname") if refer else "",
        "refer_md5": refer_md5,
        "refer_content": refer_content,
        "refer_time": _xml_tag(refer, "createtime") if refer else "",
    }


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
    if info["refer_md5"]:
        lid = wx.image_local_id_by_ref(group_id, info["refer_md5"], info["refer_time"])
        img = image_segment(wx.image_path(group_id, lid), max_bytes) if lid else None
        if img:
            segs.extend(img)
            log.info(
                "[卡片] 已附上被引用的图片（md5=%s time=%s local_id=%s）",
                info["refer_md5"],
                info["refer_time"],
                lid,
            )
        else:
            log.info(
                "[卡片] 被引用的图片拿不到本地副本（md5=%s time=%s）",
                info["refer_md5"],
                info["refer_time"],
            )

    title = info["title"] or ""
    if info["kind"] == "引用回复":
        if info["refer_author"]:
            what = "图片" if info["refer_md5"] else "消息"
            quoted = (info.get("refer_content") or "").replace("\n", " ").strip()
            if quoted:
                title = (
                    f"（引用 {info['refer_author']} 的{what}："
                    f"「{quoted[:100]}」）{title}"
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
        label = f"[{info['kind']}] {title}".strip()
        if info["des"]:
            label += f" — {info['des'][:120]}"
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
        max_edge = int(MEDIA_OPTS.get("image_max_edge", 1280))
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
        segs = image_segment(wx.image_path(group_id, m.get("local_id")), max_bytes)
        if segs:
            return segs
        return [{"type": "text", "data": {"text": "[图片（本机没有可解密的副本）]"}}]

    if "表情" in mtype:
        if MEDIA_OPTS.get("fetch_stickers", True):
            try:
                segs = image_segment(
                    wx.sticker_frame(group_id, m.get("local_id")), max_bytes
                )
                if segs:
                    return segs
            except Exception as e:  # noqa: BLE001
                # 反过来兜底：取图这条路炸了也只退化为占位，
                # **不能**把整条消息丢掉（那是「不认识表情包」的成因）
                log.warning("表情包取图异常（local_id=%s）: %s", m.get("local_id"), e)
        return [{"type": "text", "data": {"text": "[表情包]"}}]

    if "卡片" in mtype or "链接" in mtype:
        return card_segments(
            m.get("text") or "", group_id, wx, max_bytes, nicknames, self_id
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
        )
    except Exception as e:  # noqa: BLE001
        log.error("初始化微信失败（微信是否已登录并保持运行？）: %s", e)
        return 1

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

    # 启动时对齐水位，避免把历史消息当新消息
    last_seq: dict[str, int] = {}
    for g in groups:
        last_seq[g["id"]] = wx.latest_seq(g["id"])
    log.info("水位基线已对齐")

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

    async def handle_action(action: str, params: dict):
        if action == "get_login_info":
            return {"user_id": cfg["astrbot"].get("self_id", 10001), "nickname": wx.self_name}
        if action == "get_version_info":
            return {
                "app_name": "webot-bridge",
                "app_version": "0.1.0",
                "protocol_version": "v11",
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
        if action == "can_send_image":
            return {"yes": True}
        if action == "can_send_record":
            return {"yes": False}
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
                return {"message_id": 0}
            # 模型写了 @某人 → 换成微信**真实 @**（对方会收到提醒）。
            # 只有名字确实是本群成员时才走 @，否则按普通文本发。
            # before/after 是 @ 的前后两段：@ 令牌要插在模型写的**原位置**，
            # 不能补到结尾（否则句首的 @ 会糊到句尾，看起来就是「乱 @」）。
            at_name, before, after = find_mention(
                text, wx.member_names(_wxid_of(gid))
            )
            if at_name and (before.strip() or after.strip()):
                log.info(
                    "[发送@] %s -> %s: %s @ %s",
                    name,
                    at_name,
                    before[:40],
                    after[:40],
                )
                ok = await asyncio.to_thread(wx.send_at, name, at_name, before, after)
            else:
                ok = await asyncio.to_thread(wx.send_text, name, text)
            return {"message_id": 1 if ok else 0}
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
                return {"message_id": 0}
            log.info("[发送] 合并转发 → 按普通长消息发出（%d 字）", len(text))
            ok = await asyncio.to_thread(wx.send_text, name, text)
            return {"message_id": 1 if ok else 0}

        # 未实现的动作：**发送类**必须告警（静默失败极难排查），查询类记 debug
        if action.startswith("send_"):
            log.warning(
                "[动作] 未实现的发送动作：%s（参数键=%s）——这条消息不会发出去",
                action,
                list(params.keys())[:8],
            )
        else:
            log.debug("未处理的 action: %s", action)
        return None

    def _fake_id(group_id: str) -> int:
        """把微信 wxid 稳定地映射成 OneBot 的数字 ID。

        注意：不能用内置 hash() —— Python 对 str 的 hash 每个进程都会加盐随机，
        桥一重启同一个群的 ID 就会变，导致 AstrBot 的会话/窗口状态错乱。
        """
        import zlib
        return zlib.crc32(group_id.encode("utf-8")) % (10 ** 9)

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
        token=cfg["astrbot"].get("token", ""),
        heartbeat=float(cfg["astrbot"].get("heartbeat_seconds", 15)),
        action_handler=handle_action,
    )
    asyncio.create_task(bot.run())

    # ── 轮询上报 ──────────────────────────────────────
    interval = float(cfg["wechat"].get("poll_interval", 1.5))
    send_images = bool(cfg["wechat"].get("send_images", True))
    image_max_bytes = int(cfg["wechat"].get("image_max_bytes", 3_000_000))
    while True:
        for g in groups:
            try:
                msgs = await asyncio.to_thread(wx.new_messages, g["id"], last_seq[g["id"]])
            except Exception as e:  # noqa: BLE001
                log.warning("读取群[%s]异常: %s", g["name"], e)
                continue
            for m in msgs:
                last_seq[g["id"]] = max(last_seq[g["id"]], m["seq"])

                mtype = str(m.get("type") or "")

                # ── 非文本：图片 / 表情包 / 语音 / 链接 ────────────────
                # 图片要先解密落到本地再读成 base64，是磁盘 IO —— 放线程里做，
                # 否则会堵住轮询循环、拖慢整个群的消息延迟。
                if "文本" not in mtype:
                    if not send_images:
                        continue
                    try:
                        segments = await asyncio.to_thread(
                            build_media_segments,
                            m,
                            g["id"],
                            wx,
                            image_max_bytes,
                            mention_aliases,
                            cfg["astrbot"].get("self_id", 10001),
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "处理非文本消息失败（%s / 类型=%s）: %s", g["name"], mtype, e
                        )
                        continue
                    if not segments:
                        log.debug("[跳过] %s 类型=%s", g["name"], mtype)
                        continue

                    is_img = segments[0].get("type") == "image"
                    log.info(
                        "[收到] %s / %s: <%s>",
                        g["name"],
                        m["sender_name"],
                        "图片" if is_img else mtype,
                    )
                    log.info(
                        "[上报] group=%s user=%s segments=%s",
                        g["name"],
                        m["sender_name"],
                        (
                            f"<image base64 {len(segments[0]['data']['file'])} 字节>"
                            if is_img
                            else json.dumps(segments, ensure_ascii=False)
                        ),
                    )

                    await bot.send_group_message(
                        group_id=_fake_id(g["id"]),
                        segments=segments,
                        user_id=_fake_id(m["sender_wxid"] or m["sender_name"]),
                        nickname=m["sender_name"],
                    )
                    continue

                if not m["text"]:
                    continue
                if m["text"].lstrip().startswith("<"):
                    log.debug("[跳过] XML 内容: %s", m["text"][:60])
                    continue

                log.info("[收到] %s / %s: %s", g["name"], m["sender_name"], m["text"][:60])

                segments = build_segments(
                    m["text"],
                    mention_aliases,
                    cfg["astrbot"].get("self_id", 10001),
                )
                if not segments:
                    continue

                log.info(
                    "[上报] group=%s user=%s segments=%s",
                    g["name"],
                    m["sender_name"],
                    json.dumps(segments, ensure_ascii=False),
                )

                await bot.send_group_message(
                    group_id=_fake_id(g["id"]),
                    segments=segments,
                    user_id=_fake_id(m["sender_wxid"] or m["sender_name"]),
                    nickname=m["sender_name"],
                )
        await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n已退出")
