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
from functools import wraps
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from send_result import SendResult
from image_index import ImageIndex

log = logging.getLogger("wechat")


def requires_bound_account(method):
    """Stop before any send fallback when a bound desktop identity is lost."""
    @wraps(method)
    def checked(self, *args, **kwargs):
        guard = getattr(self, '_binding_guard', None)
        if guard is not None:
            from account_binding import BindingError
            try:
                guard.check()
            except BindingError:
                self._confirmed_target = None
                self._last_input_box = None
                log.error('微信账号绑定已失效，本次发送取消；请检查绑定后重启桥。')
                return SendResult('failed', 'account_binding_invalid')
        return method(self, *args, **kwargs)
    return checked


class WatermarkUnavailable(RuntimeError):
    """无法确定可信水位（数据库不可用/查询异常）。

    只由 `latest_seq(strict=True)` 抛出：调用方必须把目标**暂缓**（下次再试），
    不能退化成 0 —— 0 表示"从头读"，会把历史消息当新消息回放。
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


SENDER_RE = re.compile(r"^([A-Za-z0-9_\-]+):(?!//)\s*(.*)$", re.S)
GROUP_SUFFIX = "@chatroom"

# 会话标题的占位符尾巴：只认驱动自己声明并实测过的常量（`CHAT_NAME_SUFFIXES`）。
# 禁止从运行时观察里“学习”任何未知尾巴 —— 未知字符串不能作为会话身份依据。
_DRIVER_CHAT_SUFFIXES = ("按住鼠标 语音输入文字", "按住鼠标语音输入文字", "语音输入文字")
_driver_suffixes: tuple[str, ...] | None = None


def driver_chat_suffixes() -> tuple[str, ...]:
    """取驱动声明的占位符常量；取不到时退回到与驱动一致的固定三串。"""
    global _driver_suffixes
    if _driver_suffixes is None:
        try:
            from wechatauto.uia_driver import CHAT_NAME_SUFFIXES as _suffixes
            _driver_suffixes = tuple(str(x) for x in _suffixes if x)
        except Exception:  # noqa: BLE001
            _driver_suffixes = ()
        if not _driver_suffixes:
            _driver_suffixes = _DRIVER_CHAT_SUFFIXES
    return _driver_suffixes


def clean_chat_title(text: str | None) -> str:
    """按驱动已验证的占位符常量清洗标题；未命中就原样返回。

    只做「整串末尾等于某个已声明占位符」的剥离，不做前缀/包含/模糊推断，
    所以清洗后的结果仍可作为严格等值比较的身份依据。
    """
    name = (text or "").strip()
    for tail in driver_chat_suffixes():
        if tail and name.endswith(tail):
            head = name[: -len(tail)].strip()
            if head:
                return head
    return name

# 动画表情转出来的静态帧缓存目录（同一个表情只下一次、只转一次）
_EMOJI_CACHE_DIR = Path(__file__).with_name("cache") / "emoji"

# 图片缓存目录：收到图时按 md5 存一份，供**以后引用旧图**时找回。
# 为什么需要：图片和表情包不一样 —— 表情包有 md5 缓存可以直接命中，图片只能回微信
# 本地库现解密（`image_local_id_by_ref` 只扫最近 300 行，微信清理后/超出窗口就找不回），
# 于是「引用几天前的一张图」会失败（2026-10-16 实测）。
_IMAGE_CACHE_DIR = Path(__file__).with_name("cache") / "image"
# 图片索引：每群独立编号、全量保留、单文件。旧的 md5/time 文件仍可被 fallback 命中。
IMAGE_INDEX = ImageIndex(
    Path(__file__).with_name("cache") / "image_index.sqlite3", _IMAGE_CACHE_DIR
)
_EMOJI_MAX_PX = 1024          # 长边上限：控 token，也控 base64 后的 WS 帧大小
_EMOJI_MAX_BYTES = 12_000_000  # 下载上限（实测最大的表情 GIF 2.4MB）
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


def md5s_in_packed_info(blob) -> list[str]:
    """从图片消息行的 packed_info 里取出所有 32 位十六进制串（图片 md5）。

    为什么要取「所有」：packed_info 是打包过的二进制，里面可能同时有原图与缩略图的
    md5，而引用回复里带的可能是其中任意一个 —— 每个都存一份最稳（引用路径就是拿
    refer md5 去 packed_info 里比对，见 `image_local_id_by_ref`）。
    """
    if not blob:
        return []
    data = blob.encode("utf-8", "ignore") if isinstance(blob, str) else bytes(blob)
    out: list[str] = []
    for m in re.findall(rb"[0-9a-fA-F]{32}", data):
        s = m.decode("ascii", "ignore").lower()
        if s not in out:
            out.append(s)
    return out


def cached_image(md5: str, group_wxid: str | None = None) -> Path | None:
    """按 md5 取本地缓存的图片副本（先查索引，再兼容旧命名）。"""
    if not md5:
        return None
    hit = IMAGE_INDEX.find_by_md5(str(md5).lower(), group_wxid)
    if hit:
        return Path(hit["file"])
    return _cached_image_file(f"{str(md5).lower()}")


def cached_image_by_time(
    create_time, tolerance: int = 3, group_wxid: str | None = None
) -> Path | None:
    """按「被引用图片的 createtime」取缓存副本（±tolerance 秒容差）。"""
    hit = IMAGE_INDEX.find_by_time(create_time, group_wxid, tolerance)
    if hit:
        return Path(hit["file"])
    try:
        want = int(create_time)
    except (TypeError, ValueError):
        return None
    for delta in range(0, max(0, int(tolerance)) + 1):
        for t in ({want} if delta == 0 else {want - delta, want + delta}):
            hit_path = _cached_image_file(f"t{t}")
            if hit_path:
                return hit_path
    return None


def _cached_image_file(key: str) -> Path | None:
    try:
        path = _IMAGE_CACHE_DIR / f"{key}.jpg"
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def prune_image_cache(keep: int = 200) -> None:
    """图片缓存按修改时间只留最近 keep 个（图片比表情大，默认留少一些）。"""
    if keep <= 0:
        return
    try:
        files = sorted(_IMAGE_CACHE_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except OSError:
        return
    for p in files[keep:]:
        try:
            p.unlink()
        except OSError:
            pass


def save_image_cache(path: str, md5s: list[str], create_time=None, keep: int = 200) -> int:
    """把解密好的图片存进 cache/image/，返回新写入的份数。

    同时写两份键：`<md5>.jpg`（本地 packed_info 的 md5，偶尔会被引用 XML 直接用上）
    与 `t<create_time>.jpg`（引用 XML 的 createtime 与本地 create_time 一致，是主要检索键）。
    """
    import shutil

    keys = [str(m).lower() for m in (md5s or []) if m]
    try:
        if create_time:
            keys.append(f"t{int(create_time)}")
    except (TypeError, ValueError):
        pass
    if not path or not keys or keep <= 0:
        return 0
    try:
        src = Path(path)
        if not src.is_file() or src.stat().st_size <= 0:
            return 0
        _IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        saved = 0
        for key in keys:
            dst = _IMAGE_CACHE_DIR / f"{key}.jpg"
            try:
                if dst.exists() and dst.stat().st_size > 0:
                    continue
                shutil.copyfile(src, dst)
                saved += 1
            except OSError as e:
                log.debug("写入图片缓存失败（key=%s）: %s", key, e)
        prune_image_cache(keep)
        return saved
    except OSError as e:
        log.debug("图片缓存处理失败: %s", e)
        return 0


def cached_sticker(md5: str) -> Path | None:
    """按 md5 取本地已缓存的表情包静态帧。

    用途：**引用一张表情包**时，被引用内容的 md5 就在引用 ID 里，而这张表情
    之前已经下载并转成 jpg 缓存在 cache/emoji/<md5>.jpg —— 直接拿来用，
    不必再回微信数据库找 local_id（表情包在图片表里往往查不到，2026-10-16 实测）。
    """
    if not md5:
        return None
    try:
        path = _EMOJI_CACHE_DIR / f"{str(md5).lower()}.jpg"
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


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


@dataclass(frozen=True)
class SendTarget:
    """统一发送目标描述。

    至少包含：机器人账号（`account`）、会话类型（`kind`）、稳定目标 ID（`ref`）
    和实际显示名（`display`）。昵称只是显示名，不参与主键；`key` 是上层沿用的
    兼容入口键（群名 / `private:<wxid>`），只用于缓存归属与日志，不当作身份。
    """

    account: str
    kind: str          # "group" | "private"
    ref: str           # 群稳定 ID / 好友 wxid
    display: str       # 实际显示名（仅展示与唯一性校验用）
    key: str           # 兼容入口键

    @property
    def is_private(self) -> bool:
        return self.kind == "private"


class WeChatClient:
    # 已确认目标身份的有效期：到期只做必要复核（快照/唯一性/窗口/标题），
    # 不无条件搜索会话、不切换窗口。
    _TARGET_TTL = 300.0
    # 投递确认轮询间隔（先快后慢）；总时长仍受既有 8 秒上限约束。
    _CONFIRM_DELAYS = (0.2, 0.3, 0.5, 0.7, 1.0)

    def __init__(
        self,
        bot_nickname: str = "",
        db_dir: str | None = None,
        verify_send: bool = False,
        expected_wxid: str = "",
    ) -> None:
        from wechatauto import WeChatDB
        from paths import find_xwechat_root
        from account_binding import load_binding, verify_database, BindingError, BoundDatabase
        from wechat_desktop import Desktop
        from bound_wechat_gui import create_gui

        self.bot_nickname = bot_nickname
        # verify_send=True 会回读数据库确认发送，最多额外等 8 秒（延迟大）
        self.verify_send = verify_send
        binding = load_binding()
        root = db_dir or find_xwechat_root() or binding.db_dir
        if Path(root).resolve() != Path(binding.db_dir).resolve():
            raise BindingError('wechat.db_dir 与已绑定的数据根目录不一致。')
        if expected_wxid and expected_wxid != binding.wxid:
            raise BindingError('wechat.self_wxid 与已绑定账号不一致。')
        desktop = Desktop()
        self._binding_guard = desktop.guard(binding)
        raw_db = WeChatDB(db_dir=str(root), account=binding.account)
        verify_database(binding, raw_db)
        self.db = BoundDatabase(raw_db, binding, self._binding_guard)
        self.gui = create_gui(desktop, self._binding_guard)
        # 回读确认与消息读取共用同一份已定位的数据库。
        self.gui._cached_db = self.db

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
        # 机器人刚发出的图片回声：群名 -> 截止时间。微信会把自发图片作为新消息
        # 再读回来，sender 偶尔识别成「群成员」；不拦住会让模型再次看图/描述。
        self._self_image_echo_until: dict[str, float] = {}
        # 输入框坐标缓存：同一会话连续发图时跳过 get_input_box 的重校准。
        self._last_input_box: tuple[int, int, int, int] | None = None
        self._last_input_group: str = ""
        # 统一发送目标缓存：群/私聊共用，只保存已确认的稳定目标与窗口身份。
        # 不跨进程重启恢复；账号、会话类型、稳定引用或窗口身份变化即失效。
        # 身份（账号/类型/ref/展示名唯一性/窗口句柄）与输入框坐标分开校验：
        # 坐标丢失只重新探测输入框，不会重新打开会话。
        self._confirmed_target: dict | None = None
        # 最近一次缓存失效原因，只用于日志与调用方判断是否需要安全定位。
        self._last_miss_reason: str = ""
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
            self._nick_cache = dict(self.db.nickname_map(refresh=True) or {})
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

    def latest_seq(self, group_id: str, *, strict: bool = False):
        """当前最大水位（用于启动对齐，避免重放历史）。

        三态，**必须区分"未知"与"真的是 0"**：

        - 真实空会话（查得到但没消息）→ `0`（合法起点）；
        - 查询异常 + `strict=False`（旧调用方兼容）→ `None`（未知）；
        - 查询异常 + `strict=True`（启动快照 / 私聊授权门 / 发现流程）→
          抛 `WatermarkUnavailable`。

        绝不把查询失败当成 `0`：`0` 会被当成"从头读"，把整段历史当新消息回放。
        这里不打日志（保持纯查询），由调用方记录受控原因。
        """

        try:
            msgs = self.db.get_messages(group_id, 1, 0) or []
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise WatermarkUnavailable(type(exc).__name__) from exc
            return None
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

    # 一对一私聊里永远不作为会话身份出现的固定串（系统/服务号）。
    _PRIVATE_EXCLUDED = frozenset({
        "filehelper", "fmessage", "qmessage", "tmessage", "newsapp",
        "weixin", "medianote", "floatbottle",
    })

    def refresh_private_contacts(self) -> dict[str, str]:
        """只刷新联系人快照（昵称映射），**不做会话扫描**。

        联系人是私聊身份的前置条件（`private_sessions` 用它判定"是不是真人"），
        但刷新全量昵称映射很贵：正常聊天读取路径不该每轮都付这份钱。
        """
        try:
            self._nick_cache = dict(self.db.nickname_map(refresh=True) or {})
        except Exception as e:  # noqa: BLE001
            log.warning("nickname_map 刷新失败: %s", e)
            return dict(self._nick_cache)
        return dict(self._nick_cache)

    def is_private_contact(self, wxid: str) -> bool:
        """当前快照里该 wxid 是否为一个可用的一对一联系人（不查会话表）。"""
        value = str(wxid or "")
        return bool(
            value
            and value in self._nick_cache
            and not value.endswith(GROUP_SUFFIX)
            and not value.startswith("gh_")
            and value not in self._PRIVATE_EXCLUDED
            and value != self.self_wxid
        )

    def discover_private_sessions(self, limit: int = 1000) -> list[dict]:
        """扫会话表取一对一私聊会话；**不刷新联系人快照**（低频发现专用）。"""
        sessions = self.db.get_sessions(limit=limit) or []
        return [s for s in sessions if isinstance(s, dict)
                and self.is_private_contact(s.get("username") or "")]

    def private_sessions(self, limit: int = 1000) -> list[dict]:
        """Only actual one-to-one contact sessions; service accounts are excluded."""
        self.refresh_private_contacts()
        return self.discover_private_sessions(limit)

    def _private_rows(self, wxid: str, since_seq: int) -> list[dict]:
        # real_sender_id belongs to each message shard's Name2Id, NOT the
        # unrelated message_resource.SenderName2Id numbering.
        def read(tables):
            messages = []
            for conn, table in tables:
                rows = self.db._shard_rows([(conn, table)], "WHERE sort_seq > ?",
                    (since_seq,), order_ext=self.db._MSG_ORDER_ASC, per_shard_limit=200)
                senders = {}
                for row in rows:
                    sid = row['real_sender_id']
                    if sid not in senders:
                        found = conn.execute('SELECT user_name FROM Name2Id WHERE rowid=?', (sid,)).fetchone()
                        senders[sid] = str(found[0] or '') if found else ''
                    item = self.db._msg_row_to_dict(row)
                    item['sender_username'] = senders[sid]
                    messages.append(item)
            return sorted(messages, key=lambda item: item['sort_seq'])[:200]
        return self.db._run_msg_query(wxid, read) or []

    def new_private_messages(self, wxid: str, since_seq: int) -> list[dict]:
        # Do not apply group prefix rules to private messages. Unknown direction
        # is dropped rather than treating our own echo as a new activation/user turn.
        result = []
        for row in self._private_rows(wxid, since_seq):
            sender = str(row.get("sender_username") or "")
            if sender != wxid:
                result.append({"seq": self._seq(row), "is_self": True})
                continue
            result.append({"seq": self._seq(row),
                           "local_id": row.get("local_id") or row.get("localId") or row.get("id"),
                           "type": str(row.get("type") or row.get("local_type") or ""),
                           "sender_wxid": wxid, "sender_name": self.nick(wxid),
                           "is_self": False,
                           "text": ((row.get("content") or row.get("text") or "")
                                    if isinstance(row.get("content") or row.get("text") or "", str)
                                    else _zstd_text(row.get("content") or row.get("text"))),
                           "raw": row})
        return sorted(result, key=lambda m: m["seq"])

    # ── 统一增量读取入口 ────────────────────────────────
    def read_incremental(
        self, conversation_type: str, ref: str, since_seq: int
    ) -> list[dict]:
        """按会话类型读取增量事件（群聊 / 已授权私聊同一入口）。

        - 群聊：复用 `new_messages()`（含 sender 前缀归一化与自身消息过滤）；
        - 私聊：复用 `new_private_messages()`（按消息分片的 Name2Id 判定真实发送者，
          **不套用群聊的文本前缀规则**；方向未知的行返回 `is_self=True` 交给调用方
          只推进水位、不分发）。

        返回按 `seq` 升序；空列表表示没有新事件。查询异常由具体方法记日志后返回空表，
        调用方不得把"读失败"当"没有新消息"写进日志之外的状态。
        """
        if conversation_type == "group":
            return self.new_messages(str(ref), int(since_seq))
        if conversation_type == "private":
            return self.new_private_messages(str(ref), int(since_seq))
        raise ValueError(f"unknown conversation_type: {conversation_type!r}")

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
            from media_compat import BridgeMediaDownloader

            self._md = BridgeMediaDownloader(self.db)
            try:
                self._md.detect_image_key()
                log.info("图片解密密钥探测完成")
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

    def cache_image_for_reference(self, group_id: str, local_id, path: str,
                                  keep: int = 200, sender: str = "",
                                  context: str = "") -> dict | None:
        """收到图片时就存进缓存索引（供引用旧图、AI 按编号查看）。

        返回索引记录（含本群 seq）；失败返回 None。keep 仅为兼容旧调用，索引
        不再自动裁剪（保留所有）。
        """
        try:
            keep = int(keep)
        except (TypeError, ValueError):
            keep = -1
        # 0 = 关闭图片索引（仍可发当前图，但没有编号）；其它值都保留所有
        if keep == 0:
            return None
        try:
            # local_type=3 = 图片；跨分片下 local_id 非全局唯一，带上类型更准
            row = self.db.get_message_row(group_id, local_id, local_type=3)
        except TypeError:
            row = self.db.get_message_row(group_id, local_id)
        except Exception as e:  # noqa: BLE001
            log.debug("取图片消息行失败（local_id=%s）: %s", local_id, e)
            return None
        if not row:
            return None
        md5s = md5s_in_packed_info(row.get("packed_info"))
        try:
            return IMAGE_INDEX.add(
                group_id,
                path,
                md5s,
                row.get("create_time"),
                sender=sender,
                context=context,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("图片索引写入失败（%s / local_id=%s）: %s", group_id, local_id, e)
            return None

    def get_cached_image(self, group_id: str, seq: int) -> dict | None:
        return IMAGE_INDEX.get(group_id, int(seq))

    def cache_file_for_reference(
        self,
        group_id: str,
        path: str,
        sender: str = "",
        context: str = "",
        md5s: list[str] | None = None,
        create_time: int | None = None,
        local_id=None,
    ) -> dict | None:
        """给已有本地图片文件（图片/表情包静态帧）分配本群编号。"""
        try:
            # 表情包的 normalize 结果没有 create_time；从本地消息行补上，
            # 否则引用表情包时按时间完全找不到。
            if not create_time and local_id is not None:
                try:
                    row = self.db.get_message_row(group_id, local_id)
                except Exception:  # noqa: BLE001
                    row = None
                if row:
                    create_time = row.get("create_time")
                    if not md5s:
                        md5s = md5s_in_packed_info(row.get("packed_info"))
        except Exception as e:  # noqa: BLE001
            log.debug("取表情包时间失败（%s / local_id=%s）: %s", group_id, local_id, e)
        try:
            return IMAGE_INDEX.add(
                group_id,
                path,
                md5s or [],
                create_time,
                sender=sender,
                context=context,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("图片索引写入失败（%s）: %s", group_id, e)
            return None

    def list_cached_images(
        self,
        group_id: str,
        sender: str = "",
        since: str | int | None = None,
        limit: int = 20,
    ) -> dict:
        return IMAGE_INDEX.list(group_id, sender=sender, since=since, limit=limit)

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

    def _confirm_delivery(self, parts: list[str], group_name: str, mark, mode: str) -> SendResult:
        if mark is None:
            return SendResult("unknown", "已操作发送，但未取得发送前水位；不自动重发")
        deadline = time.time() + 8
        attempt = 0
        try:
            while True:
                if all(self.gui._verify_sent(part, group_name, mode=mode, after=mark) for part in parts):
                    return SendResult("sent", "数据库已确认")
                if time.time() >= deadline:
                    return SendResult("unknown", "已操作发送，但数据库未确认；不自动重发")
                # 先快后慢，避免固定 1 秒起步的无谓等待；总上限仍是 8 秒。
                time.sleep(self._CONFIRM_DELAYS[min(attempt, len(self._CONFIRM_DELAYS) - 1)])
                attempt += 1
        except Exception as e:
            return SendResult("unknown", f"已操作发送，但回读失败：{e}；不自动重发")

    def note_self_image_sent(self, group_name: str, ttl: float = 45.0) -> None:
        if group_name:
            self._self_image_echo_until[group_name] = time.time() + max(5.0, ttl)

    def _image_watermark(self, group_name: str):
        try:
            mark = self.gui._send_watermark(group_name)
            if mark is None:
                return None
            chat = mark['username']
            for row in self.db.get_messages(chat, limit=20) or []:
                if '文本' in str(row.get('type') or ''):
                    self._normalize(row, chat)
            mark['self_senders'] = {sid for sid, identity in self._sid_map.get(chat, {}).items()
                                    if identity[1] and identity[0] == self.self_wxid}
            return mark
        except Exception as exc:
            log.debug('图片水位不可用：%s', type(exc).__name__)
            return None

    def _confirm_image_delivery(self, group_name: str, mark) -> SendResult:
        if mark is None:
            return SendResult('unknown', '图片已操作发送，但缺少发送前水位；不自动重发')
        deadline = time.time() + 8
        attempt = 0
        try:
            while time.time() < deadline:
                for row in self.db.get_messages(mark['username'], limit=20) or []:
                    seq = self._seq(row)
                    key = (seq, int(row.get('local_id') or 0))
                    if seq < mark['seq'] or key in mark['ids']:
                        continue
                    own = (row.get('sender_username') == self.self_wxid
                           or row.get('sender_id') in mark.get('self_senders', set()))
                    if '图片' in str(row.get('type') or '') and own:
                        return SendResult('sent', '数据库已确认本会话水位后的自身图片消息')
                if time.time() >= deadline:
                    break
                time.sleep(self._CONFIRM_DELAYS[min(attempt, len(self._CONFIRM_DELAYS) - 1)])
                attempt += 1
        except Exception as exc:
            log.warning('图片发送回读失败：%s', type(exc).__name__)
        return SendResult('unknown', '图片已操作发送，但数据库未确认；不自动重发')

    def consume_self_image_echo(
        self, group_name: str, sender_name: str = "", ttl: float = 45.0
    ) -> bool:
        """机器人刚发图后的第一条「群成员」图片回声，只吞一次。"""
        now = time.time()
        until = self._self_image_echo_until.get(group_name, 0.0)
        if until <= 0:
            return False
        if now > until:
            self._self_image_echo_until.pop(group_name, None)
            return False
        # 已识别出发送者名字的图片不吞，避免误伤真人刚发的图。
        if sender_name and sender_name not in ("群成员", "未知", "?"):
            return False
        self._self_image_echo_until.pop(group_name, None)
        return True

    def download_file(
        self, group_id: str, local_id, filename: str | None = None,
        attempts: int = 6, delay: float = 0.8,
        expected_size: int = 0, expected_md5: str = '', occurred_at: float = 0,
    ) -> str | None:
        """把微信收到的文件还原成本地文件；失败返回 None。

        微信 4.1 的文件消息有时用 local_id 查不到 49 类型行，且文件往往比
        消息晚约 1 秒才落到 `msg/file/YYYY-MM/<原名>`；按文件名轮询重试。
        """
        from media_compat import find_download
        name = Path(str(filename or "")).name
        if not name:
            return None
        base = Path(str(getattr(self.db, "account_dir", ""))) / "msg" / "file"
        for attempt in range(max(1, int(attempts))):
            try:
                found = find_download(base, name, expected_size=expected_size,
                                      expected_md5=expected_md5, occurred_at=occurred_at)
                if found:
                    log.info(
                        "[文件] 按文件名找到本地副本：%s（第 %d 次）",
                        found,
                        attempt + 1,
                    )
                    return found
            except Exception as e:  # noqa: BLE001
                log.warning("按文件名找文件失败（%s）: %s", name, e)
            if attempt + 1 < attempts:
                time.sleep(delay)
        return None

    def _paste_image_fast(self, box, path: str, *, allow_monochrome_fallback: bool = False) -> bool:
        """快速粘贴图片：复用输入框，草稿只等 2 秒，失败回退标准路径。"""
        self._paste_enter_attempted = False
        try:
            from wechatauto.guia import VK_A, VK_DELETE, VK_RETURN, VK_V
            from wechatauto.uia_driver import rhythm

            if not self.gui.focus_input(box):
                log.info("[发图] fast-paste 失败：focus_input(%s) 未命中", box)
                return False
            from media_compat import copy_image_to_clipboard
            copied = copy_image_to_clipboard(path)
            if not copied:
                # 微信刚读完上一次剪贴板时，OpenClipboard 可能短暂失败，重试几次即可
                for _ in range(3):
                    time.sleep(0.2)
                    if copy_image_to_clipboard(path):
                        copied = True
                        break
            if not copied:
                log.info("[发图] fast-paste 失败：剪贴板写入被占用（%s）", Path(path).name)
                return False
            # 2026-11-09 提速：固定等待从 0.25/2.0/0.8 压缩到 0.1/1.2/0.15。
            # 草稿检测是轮询，检测到就立即返回，wait 只是上限，不影响可靠性。
            time.sleep(0.1)
            self.gui._input.key(VK_A, ctrl=True)
            self.gui._input.key(VK_DELETE)
            self.gui._input.key(VK_V, ctrl=True)
            draft = self.gui._input_box_has_color_draft(box, wait=1.2)
            if not draft:
                # 有些微信版本/灰度图不会出现“彩色缩略图”，但图片草稿会把
                # 输入框撑高，导致输入框探测失败 —— 用这个信号兜底判断。
                try:
                    draft = self.gui._probe_input_box() is None
                except Exception:  # noqa: BLE001
                    draft = False
            if not draft and not allow_monochrome_fallback:
                log.info("[发图] fast-paste 失败：1.2s 内未检测到草稿（box=%s）", box)
                return False
            # Preserve the proven private bitmap path for grayscale images:
            # clipboard and focus succeeded, but the UI may expose no colour
            # signal. One Enter, followed by DB confirmation; never resend.
            rhythm.gate("send")
            self._paste_enter_attempted = True
            self.gui._input.key(VK_RETURN)
            rhythm.nap(0.15)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("[发图] fast-paste 异常: %s", e)
            return False

    def _target_parts(self, group_name: str) -> tuple[str, str, str]:
        """把发送目标拆成稳定类型/引用/展示名；昵称不是主键。"""

        if group_name.startswith("private:"):
            wxid = group_name[len("private:"):]
            return "private", wxid, self.nick(wxid)
        return "group", (self.group_id(group_name) or group_name), group_name

    @staticmethod
    def _window_identity(uia) -> tuple[str, str, int]:
        win = getattr(uia, "_win", None) or getattr(uia, "_window", None)
        handle = getattr(win, "NativeWindowHandle", None) or getattr(win, "handle", None)
        name = getattr(win, "Name", "") or getattr(win, "name", "") or ""
        # 窗口句柄优先；缺失时退化为当前 UIA 实例身份，绝不只凭昵称复用。
        return (str(handle) if handle else "", str(name), 0 if handle else id(uia))

    def _describe_target(self, group_name: str) -> SendTarget | None:
        """把兼容入口键解析成统一目标描述；不合法（空、群 ID 当私聊）返回 None。"""
        if not group_name:
            return None
        if group_name.startswith("private:"):
            wxid = group_name[len("private:"):]
            if not wxid or wxid.endswith(GROUP_SUFFIX) or wxid == self.self_wxid:
                return None
            return SendTarget(self.self_wxid, "private", wxid, self.nick(wxid), group_name)
        return SendTarget(self.self_wxid, "group",
                          self.group_id(group_name) or group_name, group_name, group_name)

    @staticmethod
    def _strip_chat_placeholder(text: str | None) -> str:
        """标题清洗：只剥驱动已验证的占位符常量，见 `clean_chat_title()`。"""
        return clean_chat_title(text)

    def _display_is_unique(self, ref: str, display: str) -> bool:
        if not display:
            return False
        duplicates = {key for key, value in self._nick_cache.items()
                      if value == display and key != self.self_wxid}
        return duplicates == {ref}

    def _title_matches(self, target: dict, current: str | None) -> bool:
        """当前标题（剥掉占位符尾巴后）是否仍等于已确认标题。

        只做「整串相等」判定，不做前缀/包含匹配，所以「张三丰」永远不会被
        当成「张三」；发送后输入框占位符变化由 `_strip_chat_placeholder`
        的尾巴集合消化，尾巴只来自本目标发送成功后的观察。
        """
        clean = self._strip_chat_placeholder(current)
        if not clean:
            return False
        title = self._strip_chat_placeholder(target.get("title"))
        return bool(title) and clean == title

    def _read_title(self, uia, *, attempts: int = 3, delay: float = 0.15) -> str:
        """读当前会话标题；空值只做有限次重读，不因一次瞬时空值就去搜索会话。"""
        current = ""
        total = max(1, attempts)
        for attempt in range(total):
            try:
                current = (uia.current_chat() or "").strip()
            except Exception:  # noqa: BLE001
                current = ""
            if current:
                return current
            if attempt + 1 < total:
                time.sleep(delay)
        return current

    @staticmethod
    def _selected_chat_name(uia) -> tuple[bool, str]:
        """Read the selected session cell, independent of the chat input name.

        Return (supported, exact_name). An ambiguous or malformed selection
        is not a reason to accept the input field as a substitute.
        """
        try:
            win = getattr(uia, "_win", None)
            if win is None or not hasattr(win, "ListControl"):
                return False, ""
            sessions = win.ListControl(AutomationId="session_list")
            if not sessions.Exists(0.15, 0.05):
                return False, ""
            rows = [row for row in sessions.GetChildren()
                    if row.GetPropertyValue(30079) == True]
            if len(rows) != 1:
                return True, ""
            row = rows[0]
            if getattr(row, "ClassName", "") != "mmui::ChatSessionCell":
                return True, ""
            aid = str(getattr(row, "AutomationId", "") or "")
            lines = str(getattr(row, "Name", "") or "").splitlines()
            name = lines[0].strip() if lines else ""
            if name and aid == "session_item_" + name:
                return True, name
            return True, ""
        except Exception:
            return True, ""

    def _current_target_matches(self, uia, display: str, *, private: bool) -> bool:
        supported, selected = self._selected_chat_name(uia)
        if supported:
            return bool(selected) and selected == display
        current = self._read_title(uia)
        return (self._title_matches({"title": display}, current) if private
                else self._same_chat(current, display))

    @staticmethod
    def _identity_change_reason(old_identity, identity) -> str:
        reason = "window_identity_changed"
        if isinstance(old_identity, tuple) and len(old_identity) == 3:
            if old_identity[0] != identity[0]:
                reason = "window_handle_changed"
            elif old_identity[2] != identity[2]:
                reason = "uia_instance_changed"
            elif old_identity[1] != identity[1]:
                reason = "window_name_changed"
        return reason

    def _confirmed_uia(self, group_name: str):
        """复用已确认目标；失效只返回 None，由调用方决定是否安全定位。

        命中条件（全部满足）：账号未变、会话类型与稳定引用一致、私聊展示名仍唯一、
        窗口身份一致、当前标题仍属于该目标。300 秒到期只触发一次
        「快照 + 唯一性 + 窗口 + 标题」复核，不搜索会话、不切换窗口。
        窗口身份优先用真实窗口句柄，所以不同 UIA 包装实例指向同一窗口不算切换。
        """
        kind_label = "private" if group_name.startswith("private:") else "group"
        def miss(reason):
            log.info("[发送缓存] type=%s state=miss reason=%s", kind_label, reason)
            self._last_miss_reason = reason
            return None
        target = self._confirmed_target
        if not target:
            return miss("empty")
        if target.get("account") != self.self_wxid:
            return miss("account_changed")
        kind, ref, display = self._target_parts(group_name)
        if target.get("kind") != kind or target.get("ref") != ref:
            return miss("target_changed")
        verified_at = float(target.get("verified_at") or 0)
        stale = (not verified_at) or (time.time() - verified_at > self._TARGET_TTL)
        if kind == "private":
            if not display or display == ref or display == "unknown":
                return miss("display_missing")
            if target.get("display_name") != display:
                return miss("display_changed")
            if not self._display_is_unique(ref, display):
                return miss("display_not_unique")
        uia = self.gui._get_uia()
        if uia is None:
            return miss("uia_unavailable")
        identity = self._window_identity(uia)
        if target.get("window_identity") != identity:
            return miss(self._identity_change_reason(target.get("window_identity"), identity))
        if not self._current_target_matches(uia, display, private=kind == "private"):
            return miss("title_changed")
        if stale:
            # 300 秒只做必要复核：私聊刷一次联系人快照并确认展示名仍唯一（数十毫秒），
            # 继续复用窗口与输入框，不做 GUI 搜索、不切换窗口。
            if kind == "private":
                self.refresh_contacts()
                fresh = self.nick(ref)
                if fresh != display or not self._display_is_unique(ref, fresh):
                    return miss("identity_revalidate_failed")
                target["display_name"] = fresh
            target["verified_at"] = time.time()
        return uia

    def _confirm_target(self, group_name: str, uia, *, input_box=None) -> bool:
        """尝试把当前窗口登记为已确认目标；返回是否真的确认成功。

        调用方必须检查返回值：确认失败（标题空、标题不等于目标展示名、私聊展示名
        无法唯一对应）时不能继续发送，本方法也会清空旧目标与旧坐标。
        """
        kind, ref, display = self._target_parts(group_name)
        title = display
        title_ok = self._current_target_matches(uia, display, private=kind == "private")
        if kind == "private":
            title_ok = title_ok and self._display_is_unique(ref, display)
        if not title_ok:
            log.info("[发送缓存] type=%s state=store_rejected reason=%s", kind,
                     "title_empty" if not title else "title_mismatch")
            self._confirmed_target = None
            self._last_input_box = None
            self._last_input_group = ""
            return False
        if input_box is None:
            # 只接受属于当前目标的遗留坐标；绝不把上一个会话的坐标写进新目标。
            input_box = self._last_input_box if self._last_input_group == group_name else None
        identity = self._window_identity(uia)
        self._confirmed_target = {
            "account": self.self_wxid, "kind": kind, "ref": ref,
            "display_name": display, "title": title,
            "window_identity": identity,
            "verified_at": time.time(), "input_box": input_box,
            "box_window": identity if input_box is not None else None,
            "last_sent_at": 0.0,
            "key": group_name,
        }
        if input_box is not None:
            self._last_input_box = input_box
            self._last_input_group = group_name
            try:
                self.gui._last_input_box = input_box
            except Exception:  # noqa: BLE001
                pass
        return True

    def _current_window_identity(self):
        try:
            uia = self.gui._get_uia()
        except Exception:  # noqa: BLE001
            return None
        return self._window_identity(uia) if uia is not None else None

    def _target_box(self, group_name: str):
        """只返回属于同一已确认目标、同一窗口身份的坐标；否则当作坐标失效。

        身份无法确认（拿不到窗口身份）时同样视为失效：宁可重新探测，也不用
        可能属于别的会话的旧坐标。
        """
        target = self._confirmed_target
        if not target or target.get("account") != self.self_wxid:
            return None
        if target.get("key") != group_name:
            return None
        kind, ref, _display = self._target_parts(group_name)
        if target.get("kind") != kind or target.get("ref") != ref:
            return None
        if float(target.get("verified_at") or 0) <= 0:
            return None
        box = target.get("input_box")
        if box is None:
            return None
        box_window = target.get("box_window")
        current = self._current_window_identity()
        if current is None or box_window is None or current != box_window:
            # 窗口换过或无法确认：坐标作废；身份判定交给 `_confirmed_uia`。
            self._invalidate_box(group_name)
            return None
        return box

    def _window_identity_ok(self, uia, group_name: str) -> bool:
        """本次要用的 UIA 包装、已确认目标、当前窗口三者身份是否一致。"""
        target = self._confirmed_target
        if not target or target.get("key") != group_name:
            return False
        if target.get("account") != self.self_wxid:
            return False
        identity = self._window_identity(uia) if uia is not None else None
        current = self._current_window_identity()
        stored = target.get("window_identity")
        return (identity is not None and current is not None
                and identity == current == stored)

    def _invalidate_box(self, group_name: str) -> None:
        """作废已确认目标的坐标归属（窗口身份变了或确认不了时用）。"""
        target = self._confirmed_target
        if target and target.get("key") == group_name:
            target["input_box"] = None
            target["box_window"] = None
        if self._last_input_group == group_name:
            self._last_input_box = None
            self._last_input_group = None
            try:
                self.gui._last_input_box = None
            except Exception:  # noqa: BLE001
                pass

    def _ensure_box(self, uia, group_name: str):
        """返回属于「当前可信窗口」的输入框坐标；窗口身份确认不了就返回 None。

        取坐标前后各核对一次窗口身份：探测期间窗口被重建/切换时，刚拿到的坐标
        同样不可信，直接作废，让调用方走安全定位。
        """
        identity = self._window_identity(uia) if uia is not None else None
        current = self._current_window_identity()
        if identity is None or current is None or identity != current:
            self._invalidate_box(group_name)
            return None
        box = self._target_box(group_name)
        if box is not None:
            return box
        self._cache_input_box(uia, group_name)
        after = self._current_window_identity()
        if after is None or after != identity:
            self._invalidate_box(group_name)
            return None
        box = self._last_input_box if self._last_input_group == group_name else None
        if box is None:
            return None
        self._update_target_box(group_name, box)
        return box

    def _touch_target(self, group_name: str) -> None:
        """发送成功后只记“最近发送时间”，**绝不延长身份复核期限**。

        `verified_at` 只由初次可靠确认或 300 秒一次的联系人身份复核更新；
        持续聊天不能因此跳过复核（R04）。
        """
        target = self._confirmed_target
        if not target or target.get("key") != group_name:
            return
        if target.get("account") != self.self_wxid:
            return
        target["last_sent_at"] = time.time()

    def _resolve_send_target(self, group_name: str, *, locate: bool = True):
        """统一目标解析：先复用已确认目标，必要时按类型安全定位。

        返回 ``(uia, 输入框坐标, 是否命中缓存, 原因)``；原因取值为
        ``hit / box_missing / located / need_switch / target_invalid / locate_failed``
        或最近一次缓存失效原因。群聊的「切换会话」仍交给 ``gui.send_msg`` 适配层，
        私聊只走 UIA 安全定位，绝不把私聊当作群 ID 处理。
        """
        target = self._describe_target(group_name)
        if target is None:
            return None, None, False, "target_invalid"
        uia = self._confirmed_uia(group_name)
        if uia is not None:
            box = self._ensure_box(uia, group_name)
            if box is not None:
                return uia, box, True, "hit"
            if self._window_identity_ok(uia, group_name):
                # 身份仍可信，只是这次拿不到坐标：文本可直发，图片由调用方走安全定位。
                return uia, None, True, "box_missing"
            # 连窗口身份都确认不了：本次不复用，需要时重新安全定位。
            self._invalidate_box(group_name)
            if not locate:
                return None, None, False, "window_unverified"
        if not locate:
            return None, None, False, self._last_miss_reason or "miss"
        if target.is_private:
            uia = self._private_open(target.ref)
            if uia is None:
                return None, None, False, "locate_failed"
            box = self._ensure_box(uia, group_name)
            # 定位成功不等于身份确认成功：确认失败时禁止发送，也不留旧坐标。
            if not self._confirm_target(group_name, uia, input_box=box):
                return None, None, False, "target_unconfirmed"
            return uia, box, False, "located"
        return None, None, False, "need_switch"

    @staticmethod
    def _ms_label(value) -> str:
        return "unavailable" if value is None else str(max(0, int(value)))

    def _log_send_timing(self, *, op: str, group_name: str, cache_hit: bool,
                         state: str, target_ms=None, gui_ms=None,
                         confirm_ms=None, total_ms=None, reason: str = "") -> None:
        kind, _ref, _display = self._target_parts(group_name)
        known = [value for value in (target_ms, gui_ms, confirm_ms) if value is not None]
        total = total_ms if total_ms is not None else (
            sum(known) if len(known) == 3 else None)
        log.info(
            "[发送计时] op=%s type=%s cache_hit=%s target_ms=%s gui_ms=%s "
            "confirm_ms=%s total_ms=%s state=%s reason=%s",
            op, kind, bool(cache_hit), self._ms_label(target_ms),
            self._ms_label(gui_ms), self._ms_label(confirm_ms),
            self._ms_label(total), state, reason or "none",
        )

    def _paste_image_shared(self, box, path: str, *, probe: bool, allow_monochrome_fallback: bool = False):
        """群/私聊共用的位图粘贴：先按需探测，失败只在未发送阶段重定位一次。"""

        if probe:
            try:
                fresh = self.gui._probe_input_box()
            except Exception:  # noqa: BLE001
                fresh = None
            if fresh and fresh != box:
                box = fresh
        if (self._paste_image_fast(box, path, allow_monochrome_fallback=True)
                if allow_monochrome_fallback else self._paste_image_fast(box, path)):
            return box, True
        if getattr(self, "_paste_enter_attempted", False):
            # 已经按过回车，结果不确定；不允许换坐标重试，避免同一张图发两次。
            return box, False
        # _paste_image_fast 返回 False 时尚未按回车；重新定位是安全的准备阶段动作。
        try:
            fresh = self.gui._probe_input_box()
        except Exception:  # noqa: BLE001
            fresh = None
        if fresh and fresh != box and (self._paste_image_fast(fresh, path, allow_monochrome_fallback=True)
                                        if allow_monochrome_fallback else self._paste_image_fast(fresh, path)):
            return fresh, True
        return box, False

    def _private_open(self, wxid: str):
        """按稳定 wxid 安全定位私聊；展示名歧义一律拒绝。

        与旧实现的区别：不再每次全量 ``refresh_contacts()``——只有联系人快照里
        没有这个 wxid（冷启动、新好友、改名）或定位失败时才刷新一次；当前窗口
        已经是该会话时直接复用，不进搜索界面。
        """
        if not wxid or wxid.endswith(GROUP_SUFFIX) or wxid == self.self_wxid:
            return None
        for attempt in range(2):
            name = self._nick_cache.get(wxid) or ""
            if not name or name == wxid or name == "unknown":
                name = self._refresh_private_name(wxid)
                if not name:
                    return None
            # 定向校验：快照内同名 + 会话检索同名，都只能命中这一个 wxid。
            matches = {key for key, value in self._nick_cache.items()
                       if value == name and key != self.self_wxid}
            matches.update(str(item.get("username") or "")
                           for item in self.db.search_contact(name) or [])
            matches.discard("")
            if matches != {wxid}:
                # 绝不选中第一个同名结果（含同名群）；快照可能过期，最多刷新重试一次。
                if attempt == 0:
                    self.refresh_contacts()
                    continue
                return None
            uia = self.gui._get_uia()
            if uia is None:
                return None
            # 稳定身份已唯一确认：当前窗口若已经是这个会话，就不进搜索界面。
            if self._current_target_matches(uia, name, private=True):
                return uia
            if uia.open_chat(name) and self._current_target_matches(uia, name, private=True):
                return uia
            # 打开失败或标题不符：可能是联系人改名，刷新快照后最多重试一次。
            if attempt == 0:
                self.refresh_contacts()
                continue
            return None
        return None

    def _refresh_private_name(self, wxid: str) -> str:
        """只在快照里查不到该 wxid 时刷新一次；查不到合法展示名返回空串。"""
        self.refresh_contacts()
        name = self.nick(wxid)
        if not name or name == wxid or name == "unknown":
            return ""
        return name

    def _update_target_box(self, group_name: str, box) -> None:
        """更新已确认目标的坐标归属，并把坐标所属窗口一并记下。"""

        target = self._confirmed_target
        if not target or target.get("key") != group_name:
            return
        if target.get("account") != self.self_wxid:
            return
        kind, ref, _display = self._target_parts(group_name)
        if target.get("kind") != kind or target.get("ref") != ref:
            return
        target["input_box"] = box
        if box is not None:
            # 坐标只能在“当前窗口身份 == 已登记的窗口身份”时登记；否则宁可丢弃，
            # 避免出现「旧身份 + 新坐标」这种混合状态。
            current = self._current_window_identity()
            if current is None or target.get("window_identity") != current:
                target["input_box"] = None
                target["box_window"] = None
                return
            target["box_window"] = current
            self._last_input_box = box
            self._last_input_group = group_name
            try:
                self.gui._last_input_box = box
            except Exception:  # noqa: BLE001
                pass
        else:
            target["box_window"] = None

    def _private_target(self, wxid: str):
        """兼容入口：统一解析私聊目标，返回 (uia, 输入框, 是否命中缓存)。"""

        uia, box, cache_hit, _reason = self._resolve_send_target("private:" + wxid)
        return uia, box, cache_hit

    def _confirm_send(self, group_name: str, op: str, mark, *,
                      text: str | None = None) -> SendResult:
        """统一投递确认调度：按会话类型查询各自消息流水。

        语义保持 `sent / failed / unknown`：确认不到就是 `unknown`，不伪造成功，
        也不因为没确认而自动重发。群聊与私聊只是查询适配器不同。
        """
        target = self._describe_target(group_name)
        if target is not None and target.is_private:
            return self._private_confirm(target.ref, mark, text=text)
        if op == "image":
            return self._confirm_image_delivery(group_name, mark)
        if mark is None:
            return SendResult("unknown", "已操作发送，但未取得发送前水位；不自动重发")
        return self._confirm_delivery([text or ""], group_name, mark, "exact")

    def _private_mark(self, wxid: str) -> dict:
        rows = self.db.get_messages(wxid, limit=5) or []
        return {"username": wxid, "seq": max((self._seq(row) for row in rows), default=0),
                "ids": {(self._seq(row), row.get("local_id")) for row in rows}}

    def _private_confirm(self, wxid: str, mark: dict, *, text: str | None = None) -> SendResult:
        deadline = time.time() + 8
        attempt = 0
        while True:
            for row in self._private_rows(wxid, max(0, mark["seq"] - 1)):
                if self._seq(row) < mark["seq"] or (self._seq(row), row.get("local_id")) in mark["ids"]:
                    continue
                if row.get("sender_username") != self.self_wxid:
                    continue
                if text is not None and row.get("content") == text:
                    return SendResult("sent", "private_db_confirmed")
                if text is None and "图片" in str(row.get("type") or ""):
                    return SendResult("sent", "private_image_db_confirmed")
            if time.time() >= deadline:
                break
            # 先快后慢：消息通常 0.2~0.5 秒内落库，避免固定 0.5 秒起步的等待。
            time.sleep(self._CONFIRM_DELAYS[min(attempt, len(self._CONFIRM_DELAYS) - 1)])
            attempt += 1
        return SendResult("unknown", "private_delivery_unconfirmed; do_not_retry")

    def _send_private_text(self, wxid: str, text: str) -> SendResult:
        key = "private:" + wxid
        started = time.perf_counter()
        cache_hit = False
        reason = ""
        try:
            mark = self._private_mark(wxid)
            uia, box, cache_hit, reason = self._resolve_send_target(key)
            target_ms = int((time.perf_counter() - started) * 1000)
            if uia is None:
                self._log_send_timing(op="text", group_name=key, cache_hit=cache_hit,
                                      state="failed", target_ms=target_ms, reason=reason)
                return SendResult("failed", "private_target_not_unique_or_not_open")
            t_send = time.perf_counter()
            if not uia.send_text(text):
                self._log_send_timing(op="text", group_name=key, cache_hit=cache_hit,
                                      state="unknown", target_ms=target_ms,
                                      gui_ms=int((time.perf_counter() - t_send) * 1000),
                                      reason=reason)
                return SendResult("unknown", "private_send_unconfirmed; do_not_retry")
            gui_ms = int((time.perf_counter() - t_send) * 1000)
            if box is None:
                box = self._ensure_box(uia, key)
            self._update_target_box(key, box)
            t_confirm = time.perf_counter()
            outcome = self._confirm_send(key, "text", mark, text=text)
            self._touch_target(key)
            self._log_send_timing(
                op="text", group_name=key, cache_hit=cache_hit, state=outcome.state,
                target_ms=target_ms, gui_ms=gui_ms,
                confirm_ms=int((time.perf_counter() - t_confirm) * 1000),
                total_ms=int((time.perf_counter() - started) * 1000), reason=reason)
            return outcome
        except Exception as exc:
            self._log_send_timing(op="text", group_name=key, cache_hit=cache_hit,
                                  state="unknown",
                                  total_ms=int((time.perf_counter() - started) * 1000),
                                  reason=reason or "exception")
            return SendResult("unknown", "private_send_error:" + type(exc).__name__)

    def _send_private_image(self, wxid: str, path: str, *, probe: bool = True) -> SendResult:
        p = Path(path)
        if not p.is_file() or p.stat().st_size <= 0:
            return SendResult('failed', 'private_image_missing')
        key = "private:" + wxid
        started = time.perf_counter()
        cache_hit = False
        reason = ""
        try:
            # 紧跟文字之后（probe=False）：只复用已确认目标，不重置前台窗口。
            uia = box = None
            if not probe:
                uia, box, cache_hit, reason = self._resolve_send_target(key, locate=False)
            if uia is None or box is None:
                if not self.gui.ensure_visible():
                    self._log_send_timing(op="image", group_name=key, cache_hit=cache_hit,
                                          state="failed", reason=reason or "window_unavailable")
                    return SendResult('failed', 'private_image_window_unavailable')
                uia, box, cache_hit, reason = self._resolve_send_target(key)
            target_ms = int((time.perf_counter() - started) * 1000)
            if uia is None:
                self._log_send_timing(op="image", group_name=key, cache_hit=cache_hit,
                                      state="failed", target_ms=target_ms, reason=reason)
                return SendResult('failed', 'private_target_not_unique_or_not_open')
            if box is None:
                box = self._ensure_box(uia, key)
            if box is None:
                self._log_send_timing(op="image", group_name=key, cache_hit=cache_hit,
                                      state="failed", target_ms=target_ms, reason="box_missing")
                return SendResult('failed', 'private_image_input_unavailable')
            mark = self._private_mark(wxid)
            t_paste = time.perf_counter()
            box, pasted = self._paste_image_shared(box, str(p), probe=probe, allow_monochrome_fallback=True)
            gui_ms = int((time.perf_counter() - t_paste) * 1000)
            if not pasted:
                state = "unknown" if getattr(self, "_paste_enter_attempted", False) else "failed"
                self._log_send_timing(op="image", group_name=key, cache_hit=cache_hit,
                                      state=state, target_ms=target_ms, gui_ms=gui_ms,
                                      reason=reason)
                return SendResult(state, "private_image_paste_unconfirmed"
                                  if state == "unknown" else "private_image_paste_failed")
            self._update_target_box(key, box)
            t_confirm = time.perf_counter()
            outcome = self._confirm_send(key, "image", mark)
            self._touch_target(key)
            if outcome.state != 'sent':
                log.warning('private_image_delivery_unconfirmed')
            self._log_send_timing(
                op="image", group_name=key, cache_hit=cache_hit, state=outcome.state,
                target_ms=target_ms, gui_ms=gui_ms,
                confirm_ms=int((time.perf_counter() - t_confirm) * 1000),
                total_ms=int((time.perf_counter() - started) * 1000), reason=reason)
            return outcome
        except Exception as exc:
            log.warning('private_image_error:%s', type(exc).__name__)
            self._log_send_timing(op="image", group_name=key, cache_hit=cache_hit,
                                  state="unknown",
                                  total_ms=int((time.perf_counter() - started) * 1000),
                                  reason=reason or "exception")
            return SendResult('unknown', 'private_image_error:' + type(exc).__name__)

    @requires_bound_account
    def send_image(self, group_name: str, path: str,
                   verify: bool | None = None, probe: bool = True) -> SendResult:
        """发送本地图片/GIF（微信图片气泡，不是真正的 sticker 消息）。

        ``probe=False``：紧跟在文字之后发图时使用。输入框坐标刚由 ``send_text``
        缓存并验证过，再做一次 ``_probe_input_box`` 只是白花 0.2–0.5 秒（2026-11-09）。
        """
        if group_name.startswith("private:"):
            return self._send_private_image(group_name[len("private:"):], path, probe=probe)
        p = Path(path)
        if not p.is_file() or p.stat().st_size <= 0:
            return SendResult("failed", f"图片不存在：{path}")
        verify_final = self.verify_send if verify is None else bool(verify)
        # 必须在发送动作之前打标记：微信可能在 UIA 回车过程中就把自发图片读回来。
        self.note_self_image_sent(group_name)
        # 回读确认与发送方式分开：开启确认也使用刚发文字时的真实输入框。
        image_mark = self._image_watermark(group_name) if verify_final else None
        started = time.perf_counter()
        cache_hit = False
        target_ms = None
        attempted = False
        if callable(getattr(self.gui, "_get_uia", None)):
            try:
                # 与文字共用同一目标解析：命中只复用坐标，未命中才看当前窗口。
                uia, box, cache_hit, _reason = self._resolve_send_target(
                    group_name, locate=False)
                if uia is None:
                    uia = self.gui._get_uia()
                    if not (uia is not None and self._current_target_matches(uia, group_name, private=False)):
                        uia = None
                target_ms = int((time.perf_counter() - started) * 1000)
                if uia is not None:
                    # 只使用属于当前已确认目标的坐标；没有就现场重新探一次（不沿用缓存）。
                    if box is None:
                        box = self.gui._probe_input_box() if callable(
                            getattr(self.gui, "_probe_input_box", None)) else None
                    if box:
                        attempted = True
                        t_paste = time.perf_counter()
                        box, pasted = self._paste_image_shared(box, str(p), probe=probe)
                        gui_ms = int((time.perf_counter() - t_paste) * 1000)
                        if pasted:
                            self.gui._current_chat = group_name
                            self._confirm_target(group_name, uia, input_box=box)
                            self.note_self_image_sent(group_name)
                            t_confirm = time.perf_counter()
                            outcome = (self._confirm_send(group_name, "image", image_mark)
                                       if verify_final else SendResult("sent", "当前会话位图发图"))
                            self._touch_target(group_name)
                            self._log_send_timing(
                                op="image", group_name=group_name, cache_hit=cache_hit,
                                state=outcome.state, target_ms=target_ms, gui_ms=gui_ms,
                                confirm_ms=int((time.perf_counter() - t_confirm) * 1000),
                                total_ms=int((time.perf_counter() - started) * 1000))
                            return outcome
                        if getattr(self, "_paste_enter_attempted", False):
                            # 已按回车但没有确认：立即结束，绝不走备用方法。
                            self._log_send_timing(
                                op="image", group_name=group_name, cache_hit=cache_hit,
                                state="unknown", target_ms=target_ms, gui_ms=gui_ms,
                                total_ms=int((time.perf_counter() - started) * 1000))
                            return SendResult("unknown", "图片已操作发送但未确认；不自动重发")
                        # 尚未按回车：只允许一次受控慢速回退；其 False/异常均按 unknown。
                        t_fallback = time.perf_counter()
                        try:
                            fallback_ok = bool(self.gui._paste_attachment_and_send(str(p), box))
                        except Exception:  # noqa: BLE001
                            fallback_ok = None
                        fallback_ms = int((time.perf_counter() - t_fallback) * 1000)
                        if fallback_ok is True:
                            self.gui._current_chat = group_name
                            self._confirm_target(group_name, uia, input_box=box)
                            self.note_self_image_sent(group_name)
                            t_confirm = time.perf_counter()
                            outcome = (self._confirm_send(group_name, "image", image_mark)
                                       if verify_final else SendResult("sent", "当前会话直发"))
                            self._touch_target(group_name)
                            self._log_send_timing(
                                op="image", group_name=group_name, cache_hit=cache_hit,
                                state=outcome.state, target_ms=target_ms,
                                gui_ms=gui_ms + fallback_ms,
                                confirm_ms=int((time.perf_counter() - t_confirm) * 1000),
                                total_ms=int((time.perf_counter() - started) * 1000))
                            return outcome
                        # 备用方法没有明确成功：保守 unknown，不再调用 gui.send_image。
                        self._log_send_timing(
                            op="image", group_name=group_name, cache_hit=cache_hit,
                            state="unknown", target_ms=target_ms,
                            gui_ms=gui_ms + fallback_ms,
                            total_ms=int((time.perf_counter() - started) * 1000))
                        return SendResult("unknown", "备用图片粘贴未确认；不自动重发")
                    # 目标已确认但没有可用坐标：没有发生发送动作，允许走唯一的公开发送路径。
            except Exception as e:  # noqa: BLE001
                if attempted or getattr(self, "_paste_enter_attempted", False):
                    self._log_send_timing(
                        op="image", group_name=group_name, cache_hit=cache_hit,
                        state="unknown", target_ms=target_ms,
                        total_ms=int((time.perf_counter() - started) * 1000))
                    return SendResult("unknown", "图片发送异常且可能已操作；不自动重发")
                log.debug("图片当前会话直发失败，回退 gui.send_image: %s", e)
        try:
            t_primary = time.perf_counter()
            resp = self.gui.send_image(
                str(p),
                group_name,
                verify=verify_final,
            )
            gui_ms = int((time.perf_counter() - t_primary) * 1000)
            outcome = SendResult.from_gui(resp)
            if outcome:
                self.note_self_image_sent(group_name)
            self._log_send_timing(
                op="image", group_name=group_name, cache_hit=cache_hit,
                state=outcome.state, target_ms=target_ms, gui_ms=gui_ms,
                total_ms=int((time.perf_counter() - started) * 1000))
            log.info(
                "[发送图片] %s（%s，%d 字节）: %s",
                group_name,
                p.name,
                p.stat().st_size,
                outcome.detail or outcome.state,
            )
            return outcome
        except Exception as e:  # noqa: BLE001
            log.warning("发送图片异常 %s: %s", path, e)
            self._log_send_timing(
                op="image", group_name=group_name, cache_hit=cache_hit,
                state="unknown", target_ms=target_ms,
                total_ms=int((time.perf_counter() - started) * 1000))
            return SendResult("unknown", f"发送图片异常：{e}")

    def ensure_visible(self) -> bool:
        """把微信置前，避免文字点击落到别的窗口（图片直发不受影响）。"""
        try:
            return bool(self.gui.ensure_visible(keep_topmost=False))
        except Exception as e:  # noqa: BLE001
            log.warning("微信置前失败: %s", e)
            return False

    @requires_bound_account
    def send_text_and_image(
        self, group_name: str, text: str, path: str
    ) -> dict:
        """先发文字，再在同一会话发图片（给回复标记用）。

        2026-11-09 提速：图片走 ``probe=False``（坐标刚缓存过），并记录分段耗时，
        便于确认「文字→图」的间隔到底花在哪一段。
        """
        body = str(text or "").strip()
        sent_text = False
        text_ms = 0
        if body:
            # 文字点击是前台操作；微信被挡住时 SendKeys 会打到别的窗口。
            self.ensure_visible()
            t_text = time.time()
            result = self.send_text(group_name, body)
            text_ms = int((time.time() - t_text) * 1000)
            # unknown 不能当作已发送；也不继续发图，避免文字未确认时追加媒体。
            if result.state != "sent":
                return {
                    "ok": False,
                    "sent_text": False,
                    "text_state": result.state,
                    "error": f"文字未确认，未继续发送图片：{result.detail or result.state}",
                }
            sent_text = True
        t_img = time.time()
        image_result = self.send_image(group_name, path, probe=False)
        img_ms = int((time.time() - t_img) * 1000)
        if not image_result:
            return {
                "ok": False,
                "sent_text": sent_text,
                "error": f"图片发送失败：{image_result.detail or image_result.state}",
            }
        log.info(
            "[表情计时] 文字 %dms + 图片 %dms（%s）",
            text_ms, img_ms, Path(path).name,
        )
        return {
            "ok": True,
            "sent_text": sent_text,
            "detail": "文字+图片已发送" if sent_text else "图片已发送",
        }

    def _cache_input_box(self, uia, group_name: str) -> None:
        """文字发完后缓存输入框坐标，紧接着发图时不再重新探测/校准。"""
        # A failed fresh probe must never leave another conversation's box in
        # the shared cache; callers may immediately inspect _last_input_box.
        self._last_input_box = None
        self._last_input_group = None
        try:
            self.gui._last_input_box = None
            edit = uia._chat_input(getattr(uia, "_win", None))
            rect = edit.BoundingRectangle
            ox = int(getattr(self.gui, "origin_x", 0))
            oy = int(getattr(self.gui, "origin_y", 0))
            box = (
                int(rect.left) - ox,
                int(rect.top) - oy,
                int(rect.right) - ox,
                int(rect.bottom) - oy,
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError("invalid_input_rectangle")
            self._last_input_box = box
            self._last_input_group = group_name
            self.gui._last_input_box = box
        except Exception as e:  # noqa: BLE001
            target = self._confirmed_target
            if target and target.get("key") == group_name:
                target["input_box"] = None
            log.debug("缓存输入框坐标失败: %s", e)

    @requires_bound_account
    def send_text(self, group_name: str, text: str) -> SendResult:
        """发送文本。**已在目标会话时直接发送，绝不查找 / 切换会话。**

        需求：同一个群不应该出现侧栏查找或搜索框搜索；只有当目标**不是**当前会话
        时才允许切换。对所有消息（@ 与非 @）一视同仁。

        这里在我们这一层显式判断，**不依赖 `guia.send_msg` 内部的私有缓存** ——
        它的成功路径不打日志，行为不可观测，出问题时无法定位。
        顺带这也省掉了它内部重复的一次 `current_chat()` 探测。
        """
        if group_name.startswith("private:"):
            return self._send_private_text(group_name[len("private:"):], text)
        import time as _t
        t0 = _t.time()
        cur: str | None = None
        same = False
        uia = None
        cache_hit = False
        reason = ""
        try:
            # 与图片共用同一目标解析：命中直接复用窗口/坐标，未命中才看当前窗口。
            uia, _box, cache_hit, reason = self._resolve_send_target(group_name, locate=False)
            if uia is not None:
                same = True
            else:
                uia = self.gui._get_uia()
                if uia is not None:
                    same = self._current_target_matches(uia, group_name, private=False)
        except Exception as e:  # noqa: BLE001
            log.debug("探测当前会话失败: %s", e)
            uia = None

        t_probe = _t.time() - t0
        gui_ms = None
        confirm_ms = None
        try:
            can_confirm = callable(getattr(self.gui, "_send_watermark", None)) and callable(
                getattr(self.gui, "_verify_sent", None))
            if same and uia is not None and (not self.verify_send or can_confirm):
                # 当前会话直发，确认也不切换会话。False可能发生于回车阶段。
                mark = self.gui._send_watermark(group_name) if self.verify_send else None
                t_gui = _t.perf_counter()
                sent = uia.send_text(text)
                gui_ms = int((_t.perf_counter() - t_gui) * 1000)
                if not sent:
                    ok = SendResult("unknown", "UIA未确认发送结果，已停止自动重发")
                elif self.verify_send:
                    t_confirm = _t.perf_counter()
                    ok = self._confirm_send(group_name, "text", mark, text=text)
                    confirm_ms = int((_t.perf_counter() - t_confirm) * 1000)
                else:
                    ok = SendResult("sent")
                    confirm_ms = 0
            else:
                t_gui = _t.perf_counter()
                ok = SendResult.from_gui(self.gui.send_msg(text, group_name, self.verify_send))
                gui_ms = int((_t.perf_counter() - t_gui) * 1000)
                # 切换路径内部包含发送与可选确认，无法拆分；confirm 明确 unavailable。
        except Exception as e:  # noqa: BLE001
            log.error("发送结果不确定(%s): %s", group_name, e)
            self._log_send_timing(
                op="text", group_name=group_name, cache_hit=cache_hit, state="unknown",
                target_ms=int(t_probe * 1000), gui_ms=gui_ms,
                total_ms=int((_t.time() - t0) * 1000), reason=reason or "exception")
            return SendResult("unknown", str(e))
        if not ok:
            log.warning("[发送未确认] %s: state=%s %s", group_name, ok.state, ok.detail)
            self._log_send_timing(
                op="text", group_name=group_name, cache_hit=cache_hit, state=ok.state,
                target_ms=int(t_probe * 1000), gui_ms=gui_ms, confirm_ms=confirm_ms,
                total_ms=int((_t.time() - t0) * 1000), reason=reason)
            return ok
        if uia is not None and same:
            if cache_hit:
                # 本轮开始时身份与坐标都刚校验过：只刷新复核时间，不重复探测。
                self._touch_target(group_name)
            else:
                self._cache_input_box(uia, group_name)
                self._confirm_target(group_name, uia)
        elif ok.state == "sent":
            # 切换路径成功后重新读取 UIA，只有真实当前会话一致才写入目标缓存。
            try:
                switched = self.gui._get_uia()
                if switched is not None and self._current_target_matches(switched, group_name, private=False):
                    self._cache_input_box(switched, group_name)
                    self._confirm_target(group_name, switched)
            except Exception:  # noqa: BLE001
                pass

        self._log_send_timing(
            op="text", group_name=group_name, cache_hit=cache_hit, state=ok.state,
            target_ms=int(t_probe * 1000), gui_ms=gui_ms, confirm_ms=confirm_ms,
            total_ms=int((_t.time() - t0) * 1000), reason=reason)
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
            # 统一走「打名字过滤」：@ 之后把名字粘进输入框，让微信自己把候选收窄。
            # 不再用「首屏直查 / 滚动翻页」那套 —— 成员一多就滚不到底，且滚到后面
            # 头像会被顶出可视区，点击落空（2026-11-08 用户要求全走过滤）。
            target = self._filter_to_member(uia, edit, lst, member, pop=pop)
            if target is None:
                log.info("UIA @ 名字过滤未命中 %s，本条不发 @", member)
                return False
            try:
                import uiautomation as auto

                pattern = target.GetPattern(auto.PatternId.ScrollItemPattern)
                if pattern:
                    pattern.ScrollIntoView()
                    time.sleep(0.2)
            except Exception as e:  # noqa: BLE001
                log.debug("UIA @ ScrollIntoView 失败: %s", e)
            target.Click()
            time.sleep(0.3)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("UIA @ 选人失败: %s", e)
            return False

    def _filter_to_member(self, uia, edit, lst, member: str, pop=None):
        """用「打名字过滤」在弹层里找目标；找不到就把过滤文字退掉、复原成只有 @。

        2026-11-08 修掉两处致命问题：
        1) 以前调 uia._paste_into()，它第一步是**点击输入框**拿焦点 —— 点击会让微信
           立刻收起 @ 弹层，之后 GetChildren() 自然是空的。这条路径历史上从没成功过。
           现在改为直接写剪贴板 + Ctrl+V（焦点本来就在输入框里）。
        2) _clip_set / _read_edit_value 是 WeChatGUI 的 staticmethod，不是模块级函数，
           `from wechatauto.uia_driver import _clip_set` 必然 ImportError，被 except
           吃掉后静默 return None —— 日志里连一行都不留。现在一律走 uia.xxx。
        """
        setter = getattr(uia, "_clip_set", None)
        if setter is None:
            log.info("名字过滤：WeChatGUI 没有 _clip_set，放弃")
            return None
        try:
            setter(str(member))
        except Exception as e:  # noqa: BLE001
            log.warning("名字过滤：写剪贴板失败: %s", e)
            return None

        reader = getattr(uia, "_read_edit_value", None)
        try:
            edit.SendKeys("{Ctrl}v", waitTime=0.05)
        except Exception as e:  # noqa: BLE001
            log.warning("名字过滤：Ctrl+V 失败: %s", e)
            return None

        # 粘贴有没有真的落进输入框？读不回值时不当作失败（控件可能不暴露 Value）
        time.sleep(0.25)
        got = ""
        if reader is not None:
            try:
                got = reader(edit) or ""
            except Exception:  # noqa: BLE001
                got = ""
        if got and str(member) not in got:
            log.info("名字过滤：粘贴没落进输入框（读回=%r），改用逐字输入", got[:40])
            try:
                edit.SendKeys(str(member), waitTime=0.04)
                time.sleep(0.2)
            except Exception as e:  # noqa: BLE001
                log.warning("名字过滤：逐字输入也失败: %s", e)
                return None

        # 弹层会随输入重算，列表控件可能被重建 —— 每轮重新取，别用旧句柄
        target = None
        items: list = []
        for attempt in range(4):
            time.sleep(0.45 if attempt == 0 else 0.25)
            cur = lst
            if pop is not None:
                try:
                    cand = pop.ListControl()
                    if cand.Exists(0.2, 0.1):
                        cur = cand
                except Exception:  # noqa: BLE001
                    cur = lst
            try:
                items = cur.GetChildren() or []
            except Exception:  # noqa: BLE001
                items = []
            target = self._pick_mention(items, member)
            if target is not None:
                log.info("UIA @ 用名字过滤后找到 %s（候选 %d 个）", member, len(items))
                return target
            if len(items) == 1:
                only = items[0]
                nm = (getattr(only, "Name", "") or "").strip()
                log.info(
                    "UIA @ 过滤后只剩 1 个候选（Name=%r），直接选它（目标 %s）", nm[:30], member
                )
                return only
            if items:
                log.info(
                    "UIA @ 过滤第 %d 轮候选 %r", attempt, [(getattr(i, "Name", "") or "")[:12] for i in items[:5]]
                )

        # 没找到：只在过滤文字确实进了输入框时才退格，否则会把 @ 一起吃掉
        now = ""
        if reader is not None:
            try:
                now = reader(edit) or ""
            except Exception:  # noqa: BLE001
                now = ""
        if not now or now.rstrip().endswith(str(member)):
            try:
                for _ in range(len(str(member))):
                    edit.SendKeys("{Backspace}", waitTime=0.02)
            except Exception as e:  # noqa: BLE001
                log.debug("名字过滤：退格复原失败: %s", e)
        else:
            log.info("名字过滤：输入框里没有过滤文字（读回=%r），跳过退格", now[:40])
        log.info(
            "UIA @ 名字过滤也没命中 %s（候选 %d 个：%r）",
            member,
            len(items),
            [(getattr(i, "Name", "") or "")[:12] for i in items[:5]],
        )
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
            if r + 1 >= max_rounds:
                break  # 首屏/最后一轮不滚，避免把头像顶出可视区
            try:
                lst.WheelDown(wheelTimes=1, waitTime=0.05)
            except Exception as e:  # noqa: BLE001
                log.debug("UIA @ 弹层滚动失败: %s", e)
                break
            time.sleep(0.15)
        return None, seen

    def _uia_send_at(
        self, group_name: str, member: str, before: str, after: str
    ) -> bool | SendResult:
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
        if self.verify_send and not (callable(getattr(self.gui, "_send_watermark", None)) and
                                     callable(getattr(self.gui, "_verify_sent", None))):
            return False  # old GUI versions: use public send_msg verification instead
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

        mark = self.gui._send_watermark(group_name) if self.verify_send else None
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
            log.warning("UIA 发送结果不确定: %s", e)
            return SendResult("unknown", str(e))
        self._cache_input_box(uia, group_name)
        self._confirm_target(group_name, uia)

        if self.verify_send:
            # @ tokens split before/after in the DB; verify both text portions after the watermark.
            parts = [part.strip() for part in (before, after) if part.strip()]
            outcome = self._confirm_delivery(parts, group_name, mark, "contains")
            if not outcome:
                return outcome

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

    @requires_bound_account
    def send_at(self, group_name: str, member: str, before: str, after: str) -> SendResult:
        """在目标群里 @ 成员后发送正文。**UIA 快路径优先**，失败则退化为普通发送。

        before/after：模型写的 `@昵称` 之前 / 之后的正文（见 main.find_mention）。

        为什么不再回退到 `gui.at_member`（OCR 路径）：
            它内部的 `ensure_visible()` 会**把其它窗口全部最小化**（实测每次都弹
            `自动最小化遮挡窗口 N 个`），还会去侧栏找会话 —— 这两点用户都明确反感。
            没有 @ 总比抢窗口好；失败时日志会写明原因，便于后续把 UIA 路径调稳。
        """
        outcome = self._uia_send_at(group_name, member, before, after)
        if isinstance(outcome, SendResult):
            return outcome
        if outcome:
            return SendResult("sent")
        log.warning("UIA @ 未成功，本条按普通文本发送（不发 @，也不动你的其它窗口）")
        return self.send_text(group_name, before + after)

    def _uia_send_parts(self, group_name: str, parts: list[dict]) -> bool | SendResult:
        """按「文本 / @」段顺序发送：粘文本 → 插 @ 令牌 → 粘下一段 → …。

        parts 由 main.parse_mentions 生成；每个 at 段都要走一次微信成员弹层点选。
        任何一次 @ 失败都放弃所有 @，退化为纯文本（before + after），
        避免出现「@ 令牌只插了一半」的畸形消息。
        """
        from wechatauto.uia_driver import rhythm

        t0 = time.time()
        if self.verify_send and not (
            callable(getattr(self.gui, "_send_watermark", None))
            and callable(getattr(self.gui, "_verify_sent", None))
        ):
            return False  # old GUI versions: use public send_msg verification instead
        try:
            uia = self.gui._get_uia()
        except Exception as e:  # noqa: BLE001
            log.debug("取 UIA 失败: %s", e)
            return False
        if uia is None:
            return False

        # 必须先把微信弄到前台：SendKeys 和鼠标点击都是发给前台窗口的。
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

        at_names = [p["name"] for p in parts if p.get("type") == "at"]
        text_parts = [
            str(p.get("text") or "")
            for p in parts
            if p.get("type") == "text" and str(p.get("text") or "").strip()
        ]
        at_ok = True
        first = True
        try:
            # 第一段是文本 → 粘贴时顺便聚焦并清空；第一段是 @ → 点输入框后清空。
            # 之后的文本段**不能再 _click_ctrl**：点击会把光标从刚插入的 @ 后面挪走，
            # 第二个 @ 后面的正文就会插错位置甚至丢失。只 SetFocus + Ctrl+V 追加。
            for part in parts:
                if part.get("type") == "text":
                    txt = str(part.get("text") or "")
                    if txt:
                        if first:
                            uia._paste_into(edit, txt, clear=True)
                            first = False
                        else:
                            try:
                                edit.SetFocus()
                            except Exception:  # noqa: BLE001
                                pass
                            uia._clip_set(txt)
                            edit.SendKeys("{Ctrl}v", waitTime=0.05)
                else:
                    if not self._uia_input_at(
                        uia,
                        edit,
                        str(part["name"]),
                        clear_first=first,
                    ):
                        at_ok = False
                        break
                    first = False
        except Exception as e:  # noqa: BLE001
            log.warning("UIA 多 @ 粘贴/选人失败: %s", e)
            return False

        mark = self.gui._send_watermark(group_name) if self.verify_send else None
        try:
            if not at_ok:
                # @ 失败：弹层可能还开着，回车会被它消费；先 Esc，再干净地
                # 重新粘贴「去掉所有 @ 名字」的正文。
                edit.SendKeys("{Esc}", waitTime=0.05)
                time.sleep(0.15)
                plain = "".join(
                    str(p.get("text") or "")
                    for p in parts
                    if p.get("type") == "text"
                )
                if not plain.strip():
                    log.warning("多 @ 全部失败，且没有正文可发")
                    return False
                uia._paste_into(edit, plain, clear=True)
                time.sleep(0.2)
            rhythm.gate("send")
            rhythm.nap(0.2)
            edit.SendKeys("{Enter}", waitTime=0.05)
        except Exception as e:  # noqa: BLE001
            log.warning("UIA 发送结果不确定: %s", e)
            return SendResult("unknown", str(e))
        self._cache_input_box(uia, group_name)
        self._confirm_target(group_name, uia)

        if self.verify_send:
            check = [p.strip() for p in text_parts if p.strip()]
            outcome = self._confirm_delivery(check, group_name, mark, "contains")
            if not outcome:
                return outcome

        log.info(
            "[发送@] %s -> %s（UIA %.2fs, 路径=%s, @=%s, 共%d个）: %s",
            group_name,
            "、".join(at_names),
            time.time() - t0,
            "direct" if same else "switch",
            "成" if at_ok else "没成→去掉@按纯文字发",
            len(at_names),
            "".join(str(p.get("text") or "") for p in parts)[:50],
        )
        return True

    @requires_bound_account
    def send_at_multi(self, group_name: str, parts: list[dict]) -> SendResult:
        """发送带一个或多个真实 @ 的正文；失败退化为去掉 @ 的纯文本。"""
        outcome = self._uia_send_parts(group_name, parts)
        if isinstance(outcome, SendResult):
            return outcome
        if outcome:
            return SendResult("sent")
        plain = "".join(
            str(p.get("text") or "") for p in parts if p.get("type") == "text"
        )
        log.warning(
            "UIA 多 @ 未成功，本条按普通文本发送（不发 @，也不动你的其它窗口）"
        )
        if not plain.strip():
            return SendResult("failed", "所有 @ 都未成功，且没有可发送的正文")
        return self.send_text(group_name, plain)

    def debug_dump(self, user: str, limit: int = 3) -> list[dict]:
        """调试用：打印某会话原始消息结构。"""
        try:
            return self.db.get_messages(user, limit, 0) or []
        except Exception as e:  # noqa: BLE001
            log.warning("dump(%s) 失败: %s", user, e)
            return []
