# -*- coding: utf-8 -*-
"""
封装 wechatauto-replica：读群消息 / 发消息。

依据 wechatauto-replica 1.2.4.4 的真实 API 签名：

    get_self_info() -> dict
    get_sessions(limit=100) -> list[dict]
    get_groups() -> list[dict]
    get_group_members(chatroom_wxid) -> list[dict]
    group_id_to_name(chatroom_wxid) -> str | None      # 单个查询
    group_name_to_id(name) -> str | None               # 单个查询
    get_nickname(user) -> str
    nickname_map(refresh=False) -> dict
    get_messages(user, limit=20, offset=0) -> list[dict]
    get_new_messages(user, since_seq=0, limit=200) -> list[dict]

发送侧（wechatauto.guia.WeChatGUI）：
    send_msg(text, who, verify)
    at_member(member, text, who, verify)
"""
from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("wechat")

SENDER_RE = re.compile(r"^([A-Za-z0-9_\-]+):\s*(.*)$", re.S)
GROUP_SUFFIX = "@chatroom"

# 动画表情转出来的静态帧缓存目录（同一个表情只下一次、只转一次）
_EMOJI_CACHE_DIR = Path(__file__).with_name("cache") / "emoji"
_EMOJI_MAX_PX = 1024          # 长边上限：控 token，也控 base64 后的 WS 帧大小
_EMOJI_MAX_BYTES = 12_000_000  # 下载上限（实测最大的表情 GIF 2.4MB）
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


def _zstd_text(blob) -> str:
    """微信消息正文可能是 zstd 帧（**表情包就是**）→ 解出 UTF-8 文本。

    实测（2026-10-09）：动画表情的 message_content 以 `28 b5 2f fd` 开头，
    解压后是 `<msg><emoji ... md5="..." cdnurl="..." ...>`。普通图片则不需要这一步。
    """
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return ""
    data = bytes(blob)
    if data[:4] != b"\x28\xb5\x2f\xfd":
        return data.decode("utf-8", "ignore")
    try:
        import zstandard

        out = zstandard.ZstdDecompressor().decompress(data, max_output_size=2_000_000)
        return out.decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        log.debug("zstd 解压失败（%d 字节）: %s", len(data), e)
        return ""


class WeChatClient:
    def __init__(
        self,
        bot_nickname: str = "",
        db_dir: str | None = None,
        verify_send: bool = False,
    ) -> None:
        from wechatauto import WeChatDB
        from wechatauto.guia import WeChatGUI
        from paths import find_xwechat_root

        self.bot_nickname = bot_nickname
        # verify_send=True 会回读数据库确认发送，最多额外等 8 秒（延迟大）
        self.verify_send = verify_send
        root = db_dir or find_xwechat_root()
        self.db = WeChatDB(db_dir=str(root)) if root else WeChatDB()
        self.gui = WeChatGUI()

        info = self.db.get_self_info() or {}
        self.self_wxid = info.get("username") or getattr(self.db, "wxid", "")
        self.self_name = info.get("nick_name") or info.get("remark") or "unknown"

        self._nick_cache: dict[str, str] = {}
        self._group_cache: list[dict] = []
        # 群内 sender_id -> (wxid, 是否自己)。微信库里 **图片** 的 content 是加密
        # 二进制、没有 "wxid:" 前缀，而前缀正是 _normalize 判断「谁发的」的唯一依据，
        # 所以图片会被误判成「自己发的」而丢掉。这里从**文本**消息现学映射，
        # 再反过来用于图片（文本消息两种信息都齐全）。
        self._sid_map: dict[str, dict[int, tuple[str, bool]]] = {}
        # 图片解密器：惰性创建 —— 构造 + 探测密钥有几百毫秒成本，
        # 群里几十条消息才有一张图，不值得在启动时就付这个钱。
        self._md = None
        # 群成员名缓存：群号 -> (取到的时间, 名字集合)
        self._member_cache: dict[str, tuple[float, set[str]]] = {}
        self.refresh_contacts()

        # 已配置的群名，用于把 UIA 读到的会话名解析回标准群名
        try:
            self._names: list[str] = [g["name"] for g in self.groups()]
        except Exception as e:  # noqa: BLE001
            log.warning("获取群名列表失败: %s", e)
            self._names = []

        log.info("微信账号: %s (%s)", self.self_name, self.self_wxid)

    # ── 联系人 / 群 ────────────────────────────────────
    def refresh_contacts(self) -> None:
        try:
            self._nick_cache = dict(self.db.nickname_map() or {})
        except Exception as e:  # noqa: BLE001
            log.warning("nickname_map 获取失败: %s", e)
            self._nick_cache = {}
        try:
            self._group_cache = list(self.db.get_groups() or [])
        except Exception as e:  # noqa: BLE001
            log.warning("get_groups 获取失败: %s", e)
            self._group_cache = []
        log.info("群数量: %d，昵称缓存: %d", len(self._group_cache), len(self._nick_cache))

    def groups(self) -> list[dict]:
        """返回 [{id, name}]"""
        out: list[dict] = []
        for g in self._group_cache:
            if not isinstance(g, dict):
                continue
            gid = g.get("username") or g.get("wxid") or g.get("id") or ""
            if not gid:
                continue
            name = g.get("nick_name") or g.get("nickname") or g.get("remark") or ""
            if not name:
                name = self.group_name(gid)
            out.append({"id": gid, "name": name})
        return out

    def group_name(self, group_id: str) -> str:
        try:
            n = self.db.group_id_to_name(group_id)
            if n:
                return n
        except Exception:  # noqa: BLE001
            pass
        return group_id

    def group_id(self, group_name: str) -> str:
        try:
            return self.db.group_name_to_id(group_name) or ""
        except Exception:  # noqa: BLE001
            return ""

    def nick(self, wxid: str) -> str:
        if not wxid:
            return "unknown"
        if wxid == self.self_wxid:
            return self.self_name
        if wxid in self._nick_cache:
            return self._nick_cache[wxid]
        try:
            n = self.db.get_nickname(wxid)
        except Exception:  # noqa: BLE001
            n = None
        self._nick_cache[wxid] = n or wxid
        return self._nick_cache[wxid]

    # ── 读消息 ─────────────────────────────────────────
    @staticmethod
    def _seq(msg: dict) -> int:
        for key in ("sort_seq", "seq", "sortSeq"):
            v = msg.get(key)
            if isinstance(v, int):
                return v
        return 0

    def latest_seq(self, group_id: str) -> int:
        """当前最大水位（用于启动对齐，避免重放历史）。"""
        try:
            msgs = self.db.get_messages(group_id, 1, 0) or []
        except Exception:  # noqa: BLE001
            return 0
        return self._seq(msgs[0]) if msgs else 0

    def new_messages(self, group_id: str, since_seq: int) -> list[dict]:
        try:
            raw = self.db.get_new_messages(group_id, since_seq) or []
        except Exception as e:  # noqa: BLE001
            log.warning("get_new_messages(%s) 失败: %s", group_id, e)
            return []

        out: list[dict] = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            n = self._normalize(m, group_id)
            # 跳过机器人自己发出的消息，否则会「自己回复自己」无限套娃。
            # 判别依据：微信库里「自己发的消息」**不带** "wxid:" 前缀；
            # 别人发的（含系统消息，前缀是群 id）都带前缀。
            # 例外：**图片**的 content 是加密二进制、没有前缀，靠 _sid_map 判定。
            if n["is_self"]:
                log.debug("[跳过] 自己发送的消息: %s", n["text"][:40])
                continue
            out.append(n)
        return out

    def _normalize(self, msg: dict, chat: str = "") -> dict:
        """把微信原始消息归一化。

        判别发送者：**别人**发的 content 形如 `wxid_xxx:\\n正文`，**自己**发的没有前缀。
        唯一的例外是**图片** —— content 是加密二进制、没有前缀，
        所以要用从文本消息学到的 `sender_id` 映射来判定（见 `_sid_map`）。
        """
        content = str(msg.get("content") or msg.get("text") or "")
        mtype = str(
            msg.get("type") or msg.get("msg_type") or msg.get("local_type") or ""
        )
        sid = msg.get("sender_id")
        sid = sid if isinstance(sid, int) else None
        sid_map = self._sid_map.setdefault(chat, {}) if chat else {}

        m = SENDER_RE.match(content)
        if m:
            sender_wxid, text, is_self = m.group(1), m.group(2), False
            sender_name = self.nick(sender_wxid)
            if sid is not None:
                # 文本消息两样信息都齐全 → 正好用来学映射
                sid_map[sid] = (sender_wxid, False)
        else:
            entry = sid_map.get(sid) if sid is not None else None
            text = content
            if entry is not None:
                # 学过：按学到的归属判定（对无前缀的图片同样准确）
                sender_wxid, is_self = entry[0], entry[1]
                sender_name = (
                    self.self_name
                    if is_self
                    else (self.nick(sender_wxid) if sender_wxid else "群成员")
                )
            elif "文本" in mtype:
                # 无前缀的文本 → 自己发的，顺手记下这个 sender_id
                sender_wxid, is_self, sender_name = "", True, self.self_name
                if sid is not None:
                    sid_map[sid] = (self.self_wxid, True)
            else:
                # 图片 / 表情等无前缀消息，且映射还没学到 → 保守当作群友。
                # 理由：把群友发的图丢掉是用户看得见的失败；而「把自己发的图
                # 当群友发的」不会发生 —— 桥从不发图片。
                sender_wxid, is_self, sender_name = "", False, "群成员"

        return {
            "seq": self._seq(msg),
            "local_id": msg.get("local_id") or msg.get("localId") or msg.get("id"),
            "type": mtype,
            "sender_wxid": sender_wxid,
            "sender_name": sender_name,
            "is_self": is_self,
            "text": text,
            "raw": msg,
        }

    # ── 图片解密 ──────────────────────────────────────
    def _media(self):
        """惰性创建图片解密器（构造 + 探测密钥有成本，启动时不付这个钱）。"""
        if self._md is None:
            from wechatauto.media import MediaDownloader

            self._md = MediaDownloader(self.db)
            try:
                log.info("图片解密密钥: %s", self._md.detect_image_key())
            except Exception as e:  # noqa: BLE001
                log.warning("图片解密密钥探测失败（微信需保持运行）: %s", e)
        return self._md

    def image_path(self, group_id: str, local_id, attempts: int = 3) -> str | None:
        """把某条图片消息解密成本地文件，返回绝对路径；失败返回 None。

        只走**本地解密**（读 FileStorage 里的 .dat），不走「驱动界面去下原图」——
        后者会切会话、抢焦点，还要多花几秒。本机一般只有微信下发的那份（mid），
        足够模型看清内容。

        为什么要重试：轮询间隔只有 0.8s，图片刚到的时候微信往往**还没**把可解密的
        那份写到本地，第一次会拿到 None（实测踩到过）。隔一会儿再看一次即可。
        本方法跑在线程里（见 main.py 的 asyncio.to_thread），不会堵住轮询。
        """
        if local_id is None:
            return None
        md = self._media()
        for i in range(attempts):
            path = None
            try:
                path = md.download_image(group_id, local_id)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "图片解密失败（群 %s local_id=%s，第 %d 次）: %s",
                    group_id,
                    local_id,
                    i + 1,
                    e,
                )
            if path:
                return path
            if i == 0:
                # 第一次拿不到时把三档副本情况打出来，便于判断是「还没写好」
                # 还是「微信只留了缩略图」
                try:
                    log.info(
                        "图片暂不可解密（local_id=%s），副本情况=%s",
                        local_id,
                        md.image_status(group_id, local_id),
                    )
                except Exception as e:  # noqa: BLE001
                    log.debug("image_status 失败: %s", e)
            if i < attempts - 1:
                import time as _t

                _t.sleep(1.5)
        return None

    # ── 动画表情（表情包）→ 静态帧 ───────────────────────────
    def sticker_frame(self, group_id: str, local_id, attempts: int = 2) -> str | None:
        """把「动画表情」变成一张**静态图**并返回本地路径；拿不到返回 None。

        怎么拿到的（2026-10-09 实测）：
          · 表情消息的正文是 zstd 压缩的 `<emoji>` XML，里面有 `md5` 与 `cdnurl`；
          · 本机 `cache/<月>/Emoticon/<md5前2位>/<md5>` 那份是**加密**的，
            且不是图片那套 V1/V2 容器（密钥/模式/偏移都试过，解不开）；
          · 但 XML 里的 `cdnurl`（带 filekey 的签名地址）**直接下到的就是明文**
            —— 实测 2.4MB 的 GIF、100KB 的 PNG 都拿到了。
        动图**只取第一帧**（PIL `seek(0)`）：模型看的是静态帧，
        而且原图 base64 有 3MB+，会撑爆 WS 帧（`image_max_bytes` 也会拦下）。
        结果按 md5 缓存到 `cache/emoji/<md5>.jpg`，同一个表情只下一次、只转一次。
        本方法跑在线程里（见 main.py 的 asyncio.to_thread），不会堵轮询。
        """
        if local_id is None:
            return None
        info = self._sticker_info(group_id, local_id)
        if not info:
            return None
        md5, cdnurl = info
        cached = _EMOJI_CACHE_DIR / f"{md5}.jpg"
        try:
            if cached.exists() and cached.stat().st_size > 0:
                return str(cached)
        except OSError:
            pass
        for i in range(attempts):
            try:
                path = self._fetch_sticker(cdnurl, md5)
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "表情包取图失败（md5=%s，第 %d 次）: %s", md5, i + 1, e
                )
                path = None
            if path:
                return path
            if i < attempts - 1:
                time.sleep(1.5)
        return None

    def _sticker_info(self, group_id: str, local_id) -> tuple[str, str] | None:
        """从表情消息里取出 (md5, cdnurl)；取不到返回 None。"""
        try:
            row = self.db.get_message_row(group_id, local_id)
        except Exception as e:  # noqa: BLE001
            log.debug("取表情消息行失败（local_id=%s）: %s", local_id, e)
            return None
        if not row:
            return None
        text = _zstd_text(row.get("content")) or _zstd_text(row.get("compress_content"))
        if not text:
            return None
        m = re.search(r'md5\s*=\s*"([0-9a-fA-F]{32})"', text)
        u = re.search(r'cdnurl\s*=\s*"([^"]+)"', text)
        if not m or not u:
            log.debug("表情 XML 里没有 md5/cdnurl（local_id=%s）: %s", local_id, text[:120])
            return None
        return m.group(1).lower(), html.unescape(u.group(1))

    def _fetch_sticker(self, cdnurl: str, md5: str) -> str | None:
        """下载表情原文件 → **取第一帧** 存成 jpg → 返回路径。"""
        import io
        import urllib.request

        req = urllib.request.Request(cdnurl, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read(_EMOJI_MAX_BYTES)
        if not data:
            return None
        try:
            from PIL import Image
        except Exception as e:  # noqa: BLE001
            log.warning("PIL 不可用，表情包只能退化为占位: %s", e)
            return None
        im = Image.open(io.BytesIO(data))
        frames = getattr(im, "n_frames", 1)
        try:
            im.seek(0)                      # 动图：只取第一帧
        except Exception:  # noqa: BLE001
            pass
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > _EMOJI_MAX_PX:
            scale = _EMOJI_MAX_PX / float(max(w, h))
            im = im.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS
            )
        _EMOJI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        out = _EMOJI_CACHE_DIR / f"{md5}.jpg"
        im.save(out, "JPEG", quality=88)
        try:
            size = out.stat().st_size
        except OSError:
            size = 0
        log.info(
            "表情包 → 静态图: %s（原 %d 字节/%d 帧/%dx%d → %d 字节）",
            out.name,
            len(data),
            frames,
            w,
            h,
            size,
        )
        self._prune_sticker_cache()
        return str(out)

    @staticmethod
    def _prune_sticker_cache(keep: int = 300) -> None:
        """表情缓存按修改时间只留最近 keep 个，避免无限增长。"""
        try:
            files = sorted(
                _EMOJI_CACHE_DIR.glob("*.jpg"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for p in files[keep:]:
            try:
                p.unlink()
            except OSError:
                pass

    def image_local_id_by_ref(
        self, group_id: str, md5: str | None = None, create_time=None, limit: int = 300
    ):
        """按「被引用图片」的 md5 / 时间戳找回该会话里的 local_id。

        引用回复的 XML 里只有 md5 和 createtime，没有 local_id。
        ⚠️ 实测**不能只靠 md5**：XML 里的 md5 是原始大图在 CDN 上的 md5，
        与本地存储用的那份（packed_info 里的）并不相同 —— 拿它当唯一依据会查不到。
        好在 `<createtime>` 与本地行的 create_time 是一致的，用它兜底。
        """
        if not md5 and not create_time:
            return None
        try:
            rows = self.db.get_image_rows(group_id, limit) or []
        except Exception as e:  # noqa: BLE001
            log.warning("get_image_rows 失败（找被引用图片）: %s", e)
            return None
        if md5:
            target = md5.lower().encode("ascii", "ignore")
            for row in rows:
                blob = row.get("packed_info")
                if isinstance(blob, bytes) and target in blob.lower():
                    return row.get("local_id")
        if create_time:
            try:
                want = int(create_time)
            except (TypeError, ValueError):
                want = None
            if want is not None:
                # 容差 3 秒：实测引用 XML 的 createtime 与本地行的 create_time
                # 偶尔差 1 秒，严格相等会漏（踩到过）。取时间上最接近的那条。
                best: tuple[int, object] | None = None
                for row in rows:
                    try:
                        t = int(row.get("create_time") or 0)
                    except (TypeError, ValueError):
                        continue
                    d = abs(t - want)
                    if d <= 3 and (best is None or d < best[0]):
                        best = (d, row.get("local_id"))
                if best:
                    return best[1]
        return None

    # ── 成员名单（出站 @ 用）───────────────────────────
    def member_names(self, group_id: str, ttl: float = 600.0) -> set[str]:
        """本群成员名（微信界面里显示的那个名字），带 TTL 缓存。

        为什么按**名字**而不是 wxid：微信的 @ 面板是靠 OCR 匹配显示名的
        （见 `wechatauto.guia.at_member`），给 wxid 一个都选不中。
        TTL 是为了让新入群的人过一会儿也能被 @ 到。
        """
        if not group_id:
            return set()
        now = time.time()
        cached = self._member_cache.get(group_id)
        if cached and now - cached[0] < ttl:
            return cached[1]

        names: set[str] = set()
        try:
            for mem in self.db.get_group_members(group_id) or []:
                if not isinstance(mem, dict):
                    continue
                n = (
                    mem.get("nick_name")
                    or mem.get("nickname")
                    or mem.get("remark")
                    or ""
                ).strip()
                if n:
                    names.add(n)
        except Exception as e:  # noqa: BLE001
            log.warning("获取群成员失败（%s）: %s", group_id, e)
        names.discard(self.self_name)  # 没必要 @ 自己
        if names:
            self._member_cache[group_id] = (now, names)
        return names

    # ── 发消息 ─────────────────────────────────────────
    def _same_chat(self, cur: str | None, target: str) -> bool:
        """判断 UIA 读到的「当前会话」是否就是目标群。

        不能直接用 `==`：实测 UIA 会把输入框的占位提示拼进控件 Name
        （例如 `'测试群按住鼠标 语音输入文字'`），微信的会话标题有时还带人数后缀
        （`'群名(3)'`）。这里改为「按配置群名做最长前缀匹配」：
        既覆盖这两种噪声，又不会把 `群名2` 误判成 `群名`。

        这是「同群不查找」能否生效的关键 —— 匹配不上就会去搜索会话，
        每次发送多花约 3.4 秒，并且界面上会出现侧栏查找的动作。
        """
        if not cur or not target:
            return False
        cur = cur.strip()
        hits = [n for n in (self._names or [target]) if cur == n or cur.startswith(n)]
        if not hits:
            return False
        return max(hits, key=len) == target

    def send_text(self, group_name: str, text: str) -> bool:
        """发送文本。**已在目标会话时直接发送，绝不查找 / 切换会话。**

        需求：同一个群不应该出现侧栏查找或搜索框搜索；只有当目标**不是**当前会话
        时才允许切换。对所有消息（@ 与非 @）一视同仁。

        这里在我们这一层显式判断，**不依赖 `guia.send_msg` 内部的私有缓存** ——
        它的成功路径不打日志，行为不可观测，出问题时无法定位。
        顺带这也省掉了它内部重复的一次 `current_chat()` 探测。
        """
        import time as _t
        t0 = _t.time()
        cur: str | None = None
        same = False
        uia = None
        try:
            uia = self.gui._get_uia()
            if uia is not None:
                cur = uia.current_chat()
                same = self._same_chat(cur, group_name)
        except Exception as e:  # noqa: BLE001
            log.debug("探测当前会话失败: %s", e)
            uia = None

        t_probe = _t.time() - t0
        via = "direct" if (same and uia is not None) else "switch"
        try:
            if same and uia is not None:
                # 已在目标会话：直接键入发送，**不触发任何会话查找**
                ok = bool(uia.send_text(text))
                if not ok:
                    # 直发失败 → 回退到库的完整流程（允许它去切换）
                    via = "direct-fallback"
                    self.gui.send_msg(text, group_name, self.verify_send)
                    ok = True
            else:
                # 不在目标会话（或取不到 UIA）：交给库的完整流程，允许它切换
                self.gui.send_msg(text, group_name, self.verify_send)
                ok = True
        except Exception as e:  # noqa: BLE001
            log.error("发送失败(%s): %s", group_name, e)
            return False

        log.info(
            "[发送] %s: %s (探测 %.2fs + 发送 %.2fs, 当前会话=%r, 同名=%s, 路径=%s)",
            group_name,
            text[:40],
            t_probe,
            _t.time() - t0 - t_probe,
            cur,
            same,
            via,
        )
        return ok

    def _uia_input_at(self, uia, edit, member: str, clear_first: bool = True) -> bool:
        """在输入框里 @ 一个成员（UIA 直点，不截图、不 OCR）。

        微信的成员选择弹层是**真实 UIA 控件**（AutomationId=`MentionPopover`），
        所以能直接按名字精确点中 —— 库里那条路却要全屏截图 + OCR。

        clear_first=False 用于「正文已经粘进输入框」的场景：
        此时**不能**点输入框（光标会跑到点击处）也**不能**清空，
        直接在末尾追加 `@名字` 即可 —— 粘贴动作本身已经把焦点放进输入框了。
        """
        win = getattr(uia, "_win", None)
        if win is None or edit is None:
            return False
        try:
            if clear_first:
                try:
                    uia._click_ctrl(edit)
                except Exception as e:  # noqa: BLE001
                    log.debug("点击输入框失败: %s", e)
                time.sleep(0.25)
                try:
                    edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
                except Exception:  # noqa: BLE001
                    pass
            # 只打一个 `@`，**不**接着打名字：这样弹层会列出**全部**成员，
            # 再按名字精确选中。之前连名字一起打，弹层被过滤成只剩那一个人，
            # 看起来就像「@ 不了别人」（用户看到的正是这个）。
            edit.SendKeys("@")
            pop = win.WindowControl(
                ClassName="mmui::XPopover",
                Name="Weixin",
                AutomationId="MentionPopover",
            )
            if not pop.Exists(2.5, 0.2):
                log.info("UIA @ 弹层没出现（成员=%s）", member)
                return False
            lst = pop.ListControl()
            if not lst.Exists(1.5, 0.2):
                log.info("UIA @ 弹层里没有列表控件")
                return False
            target, seen = self._find_mention_item(lst, member)
            if target is None:
                # 兜底：把名字打进输入框，微信弹层会**实时过滤**出匹配的人。
                # 成员多时列表是虚拟化的，滚轮什么时候「到底」不好判断；
                # 过滤是微信自己的搜索，比遍历可靠。
                target = self._filter_to_member(uia, edit, lst, member)
            if target is None:
                log.info(
                    "UIA @ 列表里没有 %s（滚动扫过 %d 人：%s）",
                    member,
                    len(seen),
                    seen[:8],
                )
                return False
            target.Click()
            time.sleep(0.3)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("UIA @ 选人失败: %s", e)
            return False

    def _filter_to_member(self, uia, edit, lst, member: str):
        """用「打名字过滤」在弹层里找目标；找不到就把过滤文字退掉、复原成只有 @。

        微信的 @ 弹层输入过滤是它自己的搜索：把名字粘进输入框，列表会
        收窄到匹配的几个人。失败时**必须逐字退格复原** —— 否则过滤文字会
        留在输入框里，跟正文拼在一起发出去（正文变成『正文@如月之影如月之影』）。
        """
        try:
            uia._paste_into(edit, member, clear=False)
        except Exception as e:  # noqa: BLE001
            log.debug("名字过滤：粘贴失败: %s", e)
            return None
        time.sleep(0.4)
        try:
            items = lst.GetChildren() or []
        except Exception:  # noqa: BLE001
            items = []
        target = self._pick_mention(items, member)
        if target is not None:
            log.info("UIA @ 用名字过滤后找到 %s", member)
            return target
        try:
            for _ in range(len(member)):
                edit.SendKeys("{Backspace}", waitTime=0.02)
        except Exception as e:  # noqa: BLE001
            log.debug("名字过滤：退格复原失败: %s", e)
        return None

    @staticmethod
    def _pick_mention(items, member: str):
        """在一屏候选项里挑目标：**精确同名优先**，包含匹配只作兜底。

        为什么要先扫完整屏：一屏里可能有名字互相包含的条目（比如「小泽」与
        「小泽布尔」），一边扫一边撞上谁就点谁的话，就会 @ 错人。
        """
        fallback = None
        for it in items:
            nm = (getattr(it, "Name", "") or "").strip()
            if not nm:
                continue
            if nm == member:
                return it
            if fallback is None and member in nm:
                fallback = it
        return fallback

    def _find_mention_item(self, lst, member: str, max_rounds: int = 8):
        """在 @ 成员弹层里找目标，**找不到就滚轮往下翻**再扫。

        为什么需要滚动：弹层一次只渲染前几行（实测只能 @ 到首屏显示的那几个
        头像），成员一多目标就不在首屏 —— 之前直接判定失败，于是「@ 不到人」。
        这里往下滚着找，滚到没有新名字出现（到底了）为止。
        """
        seen: list[str] = []
        seen_set: set[str] = set()
        for r in range(max_rounds):
            items = lst.GetChildren() or []
            names = [(getattr(it, "Name", "") or "").strip() for it in items]
            fresh = [n for n in names if n and n not in seen_set]
            for n in fresh:
                seen_set.add(n)
                seen.append(n)
            target = self._pick_mention(items, member)
            if target is not None:
                if r:
                    log.info(
                        "UIA @ 弹层滚动 %d 轮后找到 %s（共见 %d 人）",
                        r,
                        member,
                        len(seen),
                    )
                return target, seen
            if r and not fresh:
                break  # 滚过一轮后没有新名字 = 到底了，别白转
            try:
                lst.WheelDown(wheelTimes=3, waitTime=0.05)
            except Exception as e:  # noqa: BLE001
                log.debug("UIA @ 弹层滚动失败: %s", e)
                break
            time.sleep(0.25)
        return None, seen

    def _uia_send_at(
        self, group_name: str, member: str, before: str, after: str
    ) -> bool:
        """UIA 快路径：确保在目标群（已在就不切）→ 粘前段 → 插 @ 令牌 → 粘后段 → 回车。

        **顺序为什么是「前段 → @ → 后段」**：微信的 @ 令牌只能在光标处
        「边打边选」地插入。之前是「粘全文（去掉@）→ 再把 @ 补到结尾」，
        实测用户看到「@ 总是糊在消息末尾，像乱 @」—— 模型写在句首的 @
        被搬到了句尾。现在按模型写的位置插入，@ 在哪就还在哪。

        为什么不用库里的 `gui.at_member`：它每次都从头做「让窗口可见
        （还会**最小化其它窗口**）→ `open_chat` 重新打开会话 → 全屏 OCR 找成员」，
        实测一次 **16–42 秒**；而我们此刻本来就在目标群里、窗口也可见，全是白付。

        注意不能用 `uia.send_text()`：它内部 `_paste_into(..., clear=True)` 会把刚
        插进去的 @ 令牌一起清掉，所以这里自己拼「粘贴 + 回车」。
        """
        from wechatauto.uia_driver import rhythm

        t0 = time.time()
        try:
            uia = self.gui._get_uia()
        except Exception as e:  # noqa: BLE001
            log.debug("取 UIA 失败: %s", e)
            return False
        if uia is None:
            return False

        # ⚠️ 必须先把微信弄到**前台**：SendKeys 和鼠标点击都是发给前台窗口的。
        # 微信在后台时，@ 会打进你正在用的浏览器/编辑器，成员弹层当然不出现
        #（实测就是这个规律：你看微信时成功，一切走就必失败）。
        # 这里用 uia.ensure_window() 而**不是** guia.ensure_visible()：
        # 前者只把微信置前，后者会把你其它窗口**全部最小化**（用户明确反感）。
        try:
            if not uia.ensure_window():
                log.info("UIA @ 置前失败：微信窗口不可用（锁屏？）")
                return False
        except Exception as e:  # noqa: BLE001
            log.warning("UIA @ 置前异常: %s", e)
            return False

        same = False
        try:
            same = self._same_chat(uia.current_chat(), group_name)
        except Exception as e:  # noqa: BLE001
            log.debug("探测当前会话失败: %s", e)

        edit = None
        try:
            edit = uia._chat_input(getattr(uia, "_win", None))
        except Exception as e:  # noqa: BLE001
            log.debug("定位输入框失败: %s", e)
        if edit is None:
            return False

        if not same:
            try:
                if not uia.open_chat(group_name):
                    return False
            except Exception as e:  # noqa: BLE001
                log.debug("open_chat 失败: %s", e)
                return False

        # ① 先粘**前段**（@ 之前的正文）。@ 写在句首时前段为空 —— 那也得
        #    先清空输入框（可能残留上一条），否则会拼在一起。
        # ② 插 @ 令牌：这一步最脆（弹层要出现、要点中成员）。
        # ③ 成功则把**后段**接上；失败见下面的收尾。
        text = before + after
        at_ok = False
        try:
            if before:
                uia._paste_into(edit, before, clear=True)
            else:
                edit.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
            at_ok = self._uia_input_at(uia, edit, member, clear_first=False)
            if at_ok and after:
                uia._paste_into(edit, after, clear=False)
        except Exception as e:  # noqa: BLE001
            log.warning("UIA @ 粘贴/选人失败: %s", e)
            return False

        # ③ 收尾。@ 没成功时必须清干净再回车：
        #    a) 弹层还开着时回车会被它消费掉（=选中高亮的第一项）——真实事故：
        #       「机器人总 @ 到第一个成员」**且那条消息根本没发出去**；
        #    b) 输入框里此时是「前段@（+可能的过滤残留）」，做逐字退格修补
        #       要退几次是猜的（实测残留会把「@如月之影」糊在句尾发出去）——
        #       直接用完整正文**重新干净粘贴**一遍，结果确定。
        try:
            if not at_ok:
                edit.SendKeys("{Esc}", waitTime=0.05)
                time.sleep(0.15)
                uia._paste_into(edit, text, clear=True)
                time.sleep(0.2)
            rhythm.gate("send")
            rhythm.nap(0.2)
            edit.SendKeys("{Enter}", waitTime=0.05)
        except Exception as e:  # noqa: BLE001
            log.warning("UIA 发送失败: %s", e)
            return False

        log.info(
            "[发送@] %s -> %s（UIA %.2fs, 路径=%s, @=%s）: %s",
            group_name,
            member,
            time.time() - t0,
            "direct" if same else "switch",
            "成" if at_ok else "没成→去掉@按纯文字发",
            text[:50],
        )
        return True

    def send_at(self, group_name: str, member: str, before: str, after: str) -> bool:
        """在目标群里 @ 成员后发送正文。**UIA 快路径优先**，失败则退化为普通发送。

        before/after：模型写的 `@昵称` 之前 / 之后的正文（见 main.find_mention）。

        为什么不再回退到 `gui.at_member`（OCR 路径）：
            它内部的 `ensure_visible()` 会**把其它窗口全部最小化**（实测每次都弹
            `自动最小化遮挡窗口 N 个`），还会去侧栏找会话 —— 这两点用户都明确反感。
            没有 @ 总比抢窗口好；失败时日志会写明原因，便于后续把 UIA 路径调稳。
        """
        if self._uia_send_at(group_name, member, before, after):
            return True
        log.warning("UIA @ 未成功，本条按普通文本发送（不发 @，也不动你的其它窗口）")
        return self.send_text(group_name, before + after)

    def debug_dump(self, user: str, limit: int = 3) -> list[dict]:
        """调试用：打印某会话原始消息结构。"""
        try:
            return self.db.get_messages(user, limit, 0) or []
        except Exception as e:  # noqa: BLE001
            log.warning("dump(%s) 失败: %s", user, e)
            return []
