# -*- coding: utf-8 -*-
"""贝蒂联动插件（betty 平台 = 宿主机 6201 → 容器 6200 的 aiocqhttp 实例）。

职责只有三件事（见 docs/task-betty-dafeiyu-relay.md §5）：
  1. 解析贝蒂转来的请求：首行 `[贝蒂转达|group=<别名>]`，把别名映射成群会话 ID，写进本轮提示，
     让大肥鱼用 AstrBot 内置的 send_message_to_user 工具发到该群。
  2. 监听 send_message_to_user 调用：记录实际目标会话与内容；目标不是本轮允许的群时写告警日志。
     （框架的 on_using_llm_tool 钩子无法拦截调用，只能记录。）
  3. 回传：大肥鱼的最终正文通过 betty_reply 回给贝蒂；若正文为空但本轮已发出消息，回传"已发出：…"。

不替换大肥鱼的默认人格。群聊路由（astrbot_plugin_group_activation）不受影响，它只处理 default 平台。
"""
import re
import time
import uuid

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core.star.filter.event_message_type import EventMessageType

BETTY_PLATFORM_ID = "betty"
SEND_TOOL = "send_message_to_user"

# 群别名 → 会话 ID（格式 platform_id:message_type:session_id）。
# 与贝蒂 config.yaml 的 linkage.groups 保持一致。
# 群 ID = 桥的 _fake_id(wxid) = crc32(wxid) % 10^9；测试群 49453534744@chatroom → 18491516。
BETTY_GROUPS = {
    "测试群": "default:GroupMessage:18491516",
}

GROUP_ACTIVATION_NAME = "astrbot_plugin_group_activation"
BOT_NAME = "大肥鱼"  # 写回 L1 时的发言人（与群里机器人昵称一致，名字判定才能对上）

# 群 → 贝蒂（notify_betty）。白名单按发送者的 OneBot 用户 ID 判定。
# 216785116 = 主人（微信昵称 NONE）。要加人，在这里追加 ID。
NOTIFY_WHITELIST = {"216785116"}
NOTIFY_HOURLY_LIMIT = 6        # 每小时最多通知贝蒂的次数（所有人合计）
NOTIFY_TTL = 1800              # 秒；贝蒂超过这个时间还没转述就丢弃
NOTIFY_MAX_CHARS = 200
GROUP_NAMES = {"18491516": "测试群"}  # 群 ID → 贝蒂里使用的群名（与 BETTY_GROUPS 一致）
_notify_times: list[float] = []  # 最近一小时的通知时间戳（进程内）


def _plain_text(tool_args) -> str:
    return " ".join(
        str(m.get("text", "")) for m in (tool_args or {}).get("messages", [])
        if isinstance(m, dict) and m.get("type") == "plain"
    ).strip()


def _tool_result_text(tool_result) -> str:
    parts = []
    for c in getattr(tool_result, "content", None) or []:
        t = getattr(c, "text", None)
        if t:
            parts.append(str(t))
    return " ".join(parts).strip()


def _group_activation(context):
    meta = context.get_registered_star(GROUP_ACTIVATION_NAME)
    plugin = getattr(meta, "star_cls", None) if meta else None
    return plugin if plugin is not None and hasattr(plugin, "wm") else None


_HEADER = re.compile(r"^\[贝蒂转达\|group=([^\]]+)\]\s*\n?", re.MULTILINE)


def parse_request(text: str) -> tuple[str, str] | None:
    """解析贝蒂请求。返回 (群别名, 正文)；不是贝蒂请求返回 None。"""
    m = _HEADER.match(text or "")
    if not m:
        return None
    alias = m.group(1).strip()
    body = (text or "")[m.end():].strip()
    return alias, body


class BettyBridge(star.Star):
    def __init__(self, context: star.Context):
        super().__init__(context)
        logger.info(f"[betty] 已加载，允许的群: {list(BETTY_GROUPS)}")

    @filter.on_llm_request()
    async def route_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if event.get_platform_id() != BETTY_PLATFORM_ID:
            return
        parsed = parse_request(event.message_str or "")
        if parsed is None:
            return
        alias, body = parsed
        session = BETTY_GROUPS.get(alias)
        if session is None:
            # 别名不在映射里：不交给 LLM 发送（在 on_decorating_result 里回复错误）
            event.set_extra("betty_error", f"群别名「{alias}」不在允许列表里")
            return
        event.set_extra("betty_session", session)
        event.set_extra("betty_alias", alias)
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n【贝蒂转达】这条请求来自贝蒂（主人昴的语音助手），她代主人请你在群「"
            f"{alias}」里传达下面的内容。\n"
            f"要求：\n"
            f"1. 调用 {SEND_TOOL} 工具发送，session 参数必须填 \"{session}\"，"
            "messages 为 [{\"type\": \"plain\", \"text\": \"<你的措辞>\"}]。\n"
            "2. 措辞用你自己的口吻，但必须保留原话里的时间、日期、数字、金额、人名和链接，不要添加原话没有的 @ 或链接，不超过 200 字。\n"
            "3. 只发一条消息。发完后，用一句话回复贝蒂你发了什么。\n"
            f"原话：{body}"
        )

    @filter.on_using_llm_tool()
    async def watch_send_tool(self, event: AstrMessageEvent, tool, tool_args):
        if event.get_platform_id() != BETTY_PLATFORM_ID:
            return
        name = getattr(tool, "name", "")
        if name != SEND_TOOL:
            return
        expected = event.get_extra("betty_session")
        target = (tool_args or {}).get("session") or ""
        if expected is None or target != expected:
            logger.warning(f"[betty] ⚠️ send_message_to_user 目标与允许的群不符: "
                           f"expected={expected} actual={target!r}")
        text = _plain_text(tool_args)
        event.set_extra("betty_sent_text", text)
        event.set_extra("betty_sent_target", target)
        logger.info(f"[betty] 大肥鱼发送 → {target} len={len(text)}")

    @filter.on_llm_tool_respond()
    async def write_back_l1(self, event: AstrMessageEvent, tool, tool_args, tool_result):
        """发送成功后，把实际发出的内容写回目标群的 L1。

        桥会把机器人自己发的消息当回声丢掉，不写回的话，群里有人接着问
        “刚才说的会几点？”时，大肥鱼的 L1 里没有这句话，接不上。
        只写大肥鱼实际发出的措辞；贝蒂的请求原文不进群记忆。
        """
        if event.get_platform_id() != BETTY_PLATFORM_ID:
            return
        if getattr(tool, "name", "") != SEND_TOOL:
            return
        result_text = _tool_result_text(tool_result)
        if getattr(tool_result, "isError", False) or result_text.lower().startswith("error"):
            logger.warning(f"[betty] 发送失败，不写回 L1: {result_text[:120]}")
            event.set_extra("betty_send_error", result_text[:200])
            return
        target = str((tool_args or {}).get("session") or "")
        if target != event.get_extra("betty_session"):
            logger.warning(f"[betty] 发送目标不是本轮允许的群，不写回 L1: {target!r}")
            return
        text = _plain_text(tool_args)
        gid = target.rsplit(":", 1)[-1]
        plugin = _group_activation(self.context)
        if plugin is None or not text or not gid:
            logger.warning(f"[betty] 无法写回 L1（plugin={plugin is not None}, gid={gid!r}）")
            return
        import time as _t
        plugin.wm.push_l1(gid, {"user": BOT_NAME, "text": f"（代贝蒂转达）{text}",
                                "ts": _t.time(), "at": False})
        event.set_extra("betty_l1_written", True)
        logger.info(f"[betty] 已写回群 {gid} 的 L1（{len(text)} 字），当前 L1 {len(plugin.wm.state(gid).l1)} 条")

    @filter.on_decorating_result()
    async def reply_to_betty(self, event: AstrMessageEvent):
        if event.get_platform_id() != BETTY_PLATFORM_ID:
            return
        result = event.get_result()
        if result is None:
            return
        body = result.get_plain_text().strip()
        err = event.get_extra("betty_error")
        send_err = event.get_extra("betty_send_error")
        if err:
            body = f"传话失败：{err}"
        elif send_err:
            body = f"传话失败：发送没有成功（{send_err}）"
        elif event.get_extra("betty_l1_written") and body:
            body = f"{body}\n[实际发出] {event.get_extra('betty_sent_text')}"
        elif not body:
            sent = event.get_extra("betty_sent_text")
            if sent is None:
                # 工具调用前的空正文：跳过，等最终回复
                logger.info("[betty] 空正文的中间结果，跳过")
                event.clear_result()
                return
            body = f"已发出：{sent}" if sent else "没有发出内容。"
        await self._reply(event, body)
        event.clear_result()

    def _betty_bot(self):
        """取 betty 平台的 aiocqhttp client（用来给贝蒂发 betty_notify）。不在线返回 None。"""
        for inst in getattr(self.context.platform_manager, "platform_insts", None) or []:
            try:
                if inst.meta().id == BETTY_PLATFORM_ID:
                    return inst.get_client()
            except Exception:  # noqa: BLE001
                continue
        return None

    @filter.llm_tool(name="notify_betty")
    async def notify_betty(self, event: AstrMessageEvent, text: str, urgency: str = "normal") -> str:
        '''让贝蒂（主人的语音助手）转告主人一句话。只有主人和白名单成员在群里 @大肥鱼 时才能用。
        Args:
            text(string): 要转告的内容，保留原话中的时间、数字、人名，不超过200字
            urgency(string): normal 或 urgent
        '''
        gid = str(event.get_group_id() or "")
        if not gid:
            return "这个工具只能在群里用。"
        sender = str(event.get_sender_id() or "")
        if sender not in NOTIFY_WHITELIST:
            return "只有主人和白名单里的人能让贝蒂开口，请回复对方：这事我不能替你去办。"
        text = str(text or "").strip()
        if not text or len(text) > NOTIFY_MAX_CHARS:
            return "内容为空或超过200字，请精简后再调用。"
        now = time.time()
        _notify_times[:] = [t for t in _notify_times if now - t < 3600]
        if len(_notify_times) >= NOTIFY_HOURLY_LIMIT:
            return "这一小时已经通知贝蒂很多次了，请稍后再说。"
        bot = self._betty_bot()
        if bot is None:
            return "贝蒂现在不在线，没能通知。"
        payload = {
            "msg_id": uuid.uuid4().hex,
            "from": str(event.get_sender_name() or sender),
            "group": GROUP_NAMES.get(gid, gid),
            "text": text,
            "urgency": "urgent" if urgency == "urgent" else "normal",
            "ttl": NOTIFY_TTL,
        }
        try:
            await bot.call_action("betty_notify", **payload)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[betty] betty_notify 失败: {e}")
            return f"通知贝蒂失败：{e}"
        _notify_times.append(now)
        logger.info(f"[betty] 已通知贝蒂 msg_id={payload['msg_id']} from={payload['from']} len={len(text)}")
        return "已交给贝蒂，她会转告主人。"

    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        turn_id_raw = event.message_obj.message_id
        try:
            turn_id = int(turn_id_raw)
        except (TypeError, ValueError):
            logger.error(f"[betty] turn_id 非整数: {turn_id_raw!r}")
            return
        try:
            await event.bot.call_action("betty_reply", turn_id=turn_id, text=text)
            logger.info(f"[betty] betty_reply turn_id={turn_id} len={len(text)}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[betty] betty_reply 失败 turn_id={turn_id}: {e}")
