# -*- coding: utf-8 -*-
"""
群聊「@ 开关 + 记忆」插件。

设计（v0.4.1）：
  · 插件**不生成任何回复** —— 回复内容全部由 AstrBot 默认 LLM 生成。
  · **@ 必回**：被 @ 的那条永远发送。
  · **@ 是窗口开关**：打开 / 续期本群的滑动空闲窗口。
  · 窗口内的普通消息同样交给默认 LLM（置 event.is_at_or_wake_command = True）。
  · 窗口内**由 LLM 自主决定是否接话**：注入「参与规则」，LLM 不打算发言时只输出
    `[PASS]`；插件在 on_decorating_result 里拦下 → 消息不发送。
    **零额外延迟**（不需要额外一次 LLM 往返；那种做法每次接话要多约 1.2 秒）。
  · 确定性规则：消息里 @ 的是**别人**（不是机器人）→ 不参与。
  · 图片等非文本：真实内容由桥上报、AstrBot 直接送多模态模型；插件只把
    `[图片]` / `[语音]` 之类的占位记进 L1，避免背景里只剩「[空]」。
  · 插件真正的产出是**记忆**（三层）：
      L1 群聊滚动背景   —— 常驻记录，窗口激活时随请求注入
      L2 多轮会话       —— 复用 AstrBot 原生 conversation，插件不介入
      L3 结构化长期记忆 —— 按群隔离的「群员画像」，窗口休眠后**异步**提炼，
                          JSON 持久化（含首次/最近时间），按群注入

为什么这样：
  AstrBot 默认 LLM 自己维护多轮会话（等价于 L2），但**没被 @ 的群聊消息不进它的上下文**，
  导致「群里聊了 20 条，最后 @ 机器人 你看看」时机器人只看到最后一句、接不上话。
  我们补这一层；同时给 LLM「保持沉默」的权利，避免窗口内无关闲聊被逐条插嘴。

⚠️ 三条硬约束（否则会抑制或重复默认 LLM）：
    不要 event.send()、不要 event.call_llm = True、不要 event.stop_event()
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import StarTools
from astrbot.core.star.filter.event_message_type import EventMessageType

from .config import PluginConfig
from .kb import KBStore
from .memory import LongTermMemory
from .window import WindowManager

VERSION = "0.11.0"
PLUGIN_NAME = "astrbot_plugin_group_activation"

# 本插件的命令名（小写）。用途见 on_group_message 里的说明：AstrBot 交给
# 命令处理器时 message_str 的**前导斜杠会被吃掉**，只靠 startswith("/") 拦不住，
# 命令会被同时当聊天送给 LLM。
OWN_COMMANDS = {
    "gaw",
    "gactx",
    "gasleep",
    "gaclear",
    "gamem",
    "gaforget",
    "gakb",
    "gakbforget",
    "garemember",
    "gareset",
    "gareload",
    "gaon",
    "gaoff",
}

# 窗口噪音判定：这些占位符单独出现时 = 纯表情/纯图片消息（见 _is_noise）
_NOISE_PLACEHOLDERS = {
    "[图片]",
    "[表情包]",
    "[语音]",
    "[视频]",
    "[文件]",
    "[链接/卡片]",
}


def _is_noise(label: str) -> bool:
    """窗口内的「噪音消息」判定：单字 / 纯占位符（纯表情、纯图片……）。

    这类消息不值得放行给 LLM —— 白花一次调用，还几乎必然换来一句多余接话
    （实况：群里刷「的」「？」这种单字，机器人也跟着嗯嗯啊啊）。被 @ 的不走这道。
    """
    t = (label or "").strip()
    if not t:
        return True
    if t in _NOISE_PLACEHOLDERS:
        return True
    cleaned = re.sub(r"\[[^\]]{1,8}\]", "", t).strip()
    return len(cleaned) <= 1


# 结尾有这些标点 = 这句话说完了
_TERMINAL_PUNCT = "。！？!?…~～.，,；;：:、 \t"


def _looks_fragment(text: str, max_chars: int = 10) -> bool:
    """判定「像是话没说完的碎片」——用于把连着发的短句并成一句话。

    为什么需要：微信里人说话是碎片式的（「这是你」+「吗」），而插件是**逐条放行**
    给 LLM 的，于是每条碎片各自触发一次生成，用户看到两条互不相关的回复
    （2026-10-09 实况）。

    规则保守：只有「短 + 结尾没标点 + 单行纯文本」才算碎片 —— 宁可漏合并
    （各自回一条），也不要把一句完整的短话拖进等待。
    """
    t = (text or "").strip()
    if not t or "\n" in t:
        return False
    if len(t) > max_chars:
        return False
    if t.endswith(tuple(_TERMINAL_PUNCT)):
        return False
    if t in _NOISE_PLACEHOLDERS or t.startswith("["):
        return False
    return True


def _is_contentless(label: str) -> bool:
    """「无内容消息」判定：图片 / 表情包 / 语音 / 纯标点（含单个标点、`。。。`、`？？？`）。

    为什么要单独判：这些消息**没有文字语义**，判不了它跟话题有没有关系。
    旧规则只拦「单字」（`_is_noise`，len<=1），于是 `。。。`、`？？？`、`!!!`
    这类照样放行给 LLM，白花一次调用、还容易换来一句多余的接话。
    现在统一走「无内容」这条线 —— 是否可以回，交给**名字**判定（见 _linked_to_bot）。
    """
    t = (label or "").strip()
    if not t:
        return True
    cleaned = re.sub(r"\[[^\]]{1,8}\]", "", t).strip()   # 去掉 [图片]/[表情包] 等占位符
    if not cleaned:
        return True
    # 把标点 / 符号 / emoji 全部去掉后还剩不剩「字」
    return not re.sub(r"[\W_]+", "", cleaned)


class GroupActivationPlugin(star.Star):
    """@ 开关 + 群聊记忆（L1 滚动背景 + L3 结构化长期记忆）。"""

    def __init__(self, context: star.Context) -> None:
        self.context = context
        self.cfg = PluginConfig.load()
        self.wm = WindowManager(self.cfg)

        # L3 长期记忆：落在 data/plugin_data/<plugin>/，容器重启不丢
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[group_activation] 取插件数据目录失败，长期记忆退回插件目录: %s", e
            )
            data_dir = __import__("pathlib").Path(__file__).parent / "data"
        self.ltm = LongTermMemory(self.cfg, data_dir, logger)
        # L4 群公共知识库：写入靠窗口休眠后的提炼，读取靠 on_llm_request 检索注入
        self.kbs = KBStore(self.context, self.cfg, data_dir)
        # KB 检索预取：gid -> (query, task)。见 on_group_message ④' 与 _kb_search_for
        self._kb_prefetch: dict[str, tuple[str, asyncio.Task]] = {}
        # 每群「自己上一条回复」，供防复读闸门比对（见 _is_repeat）
        self._last_reply: dict[str, str] = {}
        # 碎片合并：key = "群:发送者"；_last_pass 记放行时刻，
        # _frag_pending 存「主句之后紧随的短句」（等 on_llm_request 来收）
        self._last_pass: dict[str, float] = {}
        self._frag_pending: dict[str, tuple[float, list[str]]] = {}
        # 「上一轮还在等续句」的截止时刻：只有在这之前的短句才该被吞掉
        # （之后上一轮已在生成/已发出，再吞就永远没人答了）
        self._merge_until: dict[str, float] = {}
        # 无内容消息（图片/表情包/标点）的判定依据：机器人自己的昵称（懒取 + 缓存）
        self._bot_names: set[str] = set()
        self._bot_name_try: float = 0.0
        # 每群「机器人刚在跟谁说话」：gid -> (昵称, 时间)。回复真正发出后记（见
        # extend_after_reply），供 _linked_to_bot 判断名字对不对得上。
        self._last_bot_talk: dict[str, tuple[str, float]] = {}
        # 「先发图、再问这是什么」：key="群:发送者" -> (图片引用, 时间)。
        # 图片只在它自己那条事件里会送给模型，后一条纯文字消息得靠这里补挂（见 inject_memory）。
        self._last_image: dict[str, tuple[list[str], float]] = {}
        # 被碎片合并吞掉的那条消息里 @ 了它 → 让父请求必须回、并 @ 回去
        self._frag_mentioned: dict[str, str] = {}
        self._l3_tasks: dict[str, asyncio.Task] = {}
        self._l3_last: dict[str, float] = {}
        self._l3_seen: dict[str, int] = {}

        logger.info(
            "[group_activation] 已加载 v%s：窗口=%.0fs 滑动=%.0fs 存活上限=%.0fs "
            "L1=%d 注入=%s 并发上限=%d ｜ L3=%s(%s) ｜ L4知识库=%s ｜ 被@自动开屏=%s",
            VERSION,
            self.cfg.w0_seconds,
            self.cfg.idle_seconds,
            self.cfg.window_max_seconds,
            self.cfg.l1_size,
            self.cfg.inject_l1,
            self.cfg.active_groups_max,
            self.cfg.l3_enable,
            self.ltm.path.name,
            self.cfg.kb_enable,
            self.cfg.wake_screen_on_at,
        )

    # ── 1. 记忆记录 + @ 开关 ──────────────────────────
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        gid = event.get_group_id() or "unknown"
        sender_id = str(event.get_sender_id() or "")
        text = (event.message_str or "").strip()
        # 图片/表情在 message_str 里是空的 —— 只记 text 的话，L1 会留下一串
        # 「[空]」，模型看不出当时有人发过图。这里补一个简短占位。
        marks = self._describe_components(event)
        label = " ".join(p for p in (text, marks) if p).strip()
        now = time.time()

        # 机器人自己发的消息：不入记忆、不放行（防止自问自答）
        if sender_id and sender_id == str(event.get_self_id()):
            return

        # 以唤醒前缀开头的命令，交给 AstrBot 内置命令体系，本插件不介入
        if any(text.startswith(p) for p in (self.cfg.ignore_prefixes or [])):
            return

        # 但 AstrBot 把「/命令」交给命令处理器时，**message_str 里的前导斜杠
        # 会被吃掉**，上面的 startswith("/") 拦不住 —— 命令会同时被当聊天
        # 送给 LLM（实测「/garemember 群主是小泽布尔」执行了命令，LLM 还回了
        # 一嘴「本鱼当不了群主」，用户看到的就是机器人把命令当聊天回）。
        # 这里按「首词是不是本插件的命令名」再拦一道。
        first_word = text.split(maxsplit=1)[0].lower() if text else ""
        if first_word.rstrip("/") in OWN_COMMANDS:
            return

        mentioned = bool(event.is_at_or_wake_command)
        sender = event.get_sender_name() or sender_id or "unknown"
        if not mentioned:
            # 兜底：自己按**消息段**再判一次是不是在叫它。
            # 为什么需要（2026-10-09 实测）：单发一个「@它」（无正文）时，框架置的
            # is_at_or_wake_command 是 False，插件于是把它当"窗口外的普通消息"沉默处理 ——
            # 连窗口都没开、L1 也没注入 → 群里表现就是「@ 了它，它却看不到你上一条」。
            why = self._wake_by_components(event)
            if why:
                mentioned = True
                event.is_at_or_wake_command = True
                logger.info(
                    "[group_activation] 群 %s：框架未标记为 @，按消息段补判为「%s」→ 视同被 @",
                    gid,
                    why,
                )

        # 被 @ = 有人在对它说话 → 标记「要开屏」，但**不在这一轮立刻切**：
        # 显示拓扑切换会让微信窗口的分辨率/DPI 变化、布局缓存失效，若此刻正在发送，
        # 输入框探测会连续失败并触发一次完整 OCR 重校准 —— 实测一次发送被拖到 **145 秒**
        # （2026-10-09 10:36:15）。所以真正动作放到 after_message_sent（回复先出去）。
        if mentioned and self.cfg.wake_screen_on_at:
            event.set_extra("ga_wake_screen", True)

        # ① L1：常驻记录群聊背景（无论窗口开没开都在记）
        #    例外：模型偶尔会把「沉默标记」当正文直接输出，那条会以正文形式进群；
        #    若再记进 L1，模型看到「机器人自己也这么说话」就会照着学 ——
        #    形成自我强化的死循环（已实际发生过）。所以这类内容一律不入记忆。
        if self.cfg.record_l1 and label.strip() != self.cfg.pass_marker.strip():
            self.wm.push_l1(
                gid,
                {"user": sender, "text": label or "[空]", "ts": now, "at": mentioned},
            )

        # ①' 图片缓存：先发图、再问「这是什么」时，**后一条是纯文字事件、拿不到图**
        #     （图片只在它自己那条事件里才会送给模型）→ 存下来，等他下一条文字消息补挂。
        #     放在所有 early return 之前：先发图（窗口外/@ 别人）再 @ 它问，也要能带上。
        frag_key = f"{gid}:{sender_id}"
        refs = self._image_refs(event)
        if refs:
            self._last_image[frag_key] = (refs, now)

        # ①'' 空 @（只 @ 了它、没有正文、也没带图）→ 把「他刚说的那句」当成本次要回的内容。
        #     微信用法：先打字再 @、或先 @ 再打字、或「问了没被理，再 @ 一次」。
        #     没有这一步，模型只会看到一条"没内容"的 @ → 回一句"本鱼在"就完了。
        if mentioned and self.cfg.bare_at_lookback_seconds > 0:
            bare = not re.sub(r"\[At:\d+\]", "", text).strip() and not marks
            if bare:
                prev = self._recent_speech(gid, sender, now)
                if prev:
                    event.set_extra("ga_bare_at_ctx", prev)
                    logger.info(
                        "[group_activation] 群 %s 空 @（%s）→ 接上他刚说的那句：%r",
                        gid,
                        sender,
                        prev[:40],
                    )

        # ② 确定性规则（不交给 LLM 判断）：消息里 @ 了**别人** → 不参与。
        #    @ 机器人会被桥转成 at 段（mentioned=True）；反之文本里还留有 "@"
        #    说明它 @ 的是别的成员（例如「@NULL 你呢」），那是在跟那个人说话。
        #    实测 LLM 会把它误判成「与我相关」而插嘴，所以这里用硬规则拦掉。
        #    仍然记入 L1（上面已 push），只是不回应、也不续期窗口。
        if "@" in text and not mentioned:
            logger.info(
                "[group_activation] 群 %s 消息里 @ 的是别人，不参与：%r",
                gid,
                text[:40],
            )
            return

        # ③ @ 开关：打开 / 续期窗口
        if mentioned:
            if not self.wm.is_active(gid, now):
                if self.wm.active_count() >= self.cfg.active_groups_max:
                    logger.info(
                        "[group_activation] 群 %s 唤醒被忽略：并发已达上限 %d",
                        gid,
                        self.cfg.active_groups_max,
                    )
                    return
                logger.info(
                    "[group_activation] 群 %s 窗口打开（被 @，初始 %.0fs）",
                    gid,
                    self.cfg.w0_seconds,
                )
            self.wm.activate(gid, now)
        elif self.wm.is_active(gid, now):
            self.wm.touch(gid, now)      # 窗口内：滑动续期
        else:
            return                        # 窗口外且未被 @：只当记忆，不出声

        # ③' 碎片合并：同一个人几秒内连着发的短句，其实**是一句话**
        #     （实况痛点：「这是你」+「吗」被当成两条独立消息，各回一次、互不相关）。
        #     * 第一条照常放行，只记下放行时刻；
        #     * 紧随其后的短句只记 L1、**不放行** —— 这段放在噪音过滤**之前**，
        #       所以「吗」这种单字也会被收进来，而不是当噪音丢掉；
        #     * 第一条那次 LLM 请求会在 on_llm_request 里先等 merge_seconds，
        #       把这几个碎片合成一句话再生成 —— 用户只看到一条回复。
        frag_like = self.cfg.merge_enable and _looks_fragment(
            label, self.cfg.fragment_max_chars
        )
        # 上一条还在等续句 —— 期间这个人发的**任何**话都并进那一轮：
        #   · 短句（碎片）：合并成一句话
        #   · @ 了它的话：也必须并进去（否则那一轮已经在生成的回复看不到这条，
        #     而这条自己又生成一条 → 两条回复，且第二条没有图/没有前面的上下文）。
        #     被吞的 @ 记在 _frag_mentioned，由父请求负责「必回 + @ 回去」。
        if (
            now < self._merge_until.get(frag_key, 0.0)
            and (frag_like or mentioned)
        ):
            _, lst = self._frag_pending.get(frag_key, (now, []))
            lst.append(label)
            self._frag_pending[frag_key] = (now, lst)
            if mentioned:
                # 这条 @ 了它 → 上面那一轮的回复必须回，并且开头 @ 回去
                self._frag_mentioned[frag_key] = sender
            logger.info(
                "[group_activation] 群 %s 碎片并入上一条（%s → %r，共 %d 段%s）",
                gid,
                sender,
                label[:20],
                len(lst) + 1,
                "，含 @ 它" if mentioned else "",
            )
            return
        # 等待窗口已过：上一轮**已经在生成、甚至已经发出**了，这条不能再吞掉
        # （吞了就永远没人答）。改为**放行** ——
        #   * 生成还没结束时：框架自己的 follow-up 机制会把它插进正在跑的那一轮，
        #     模型在下一步就看到它并重新评估（见 docs/architecture.md「碎片合并」）；
        #   * 生成已结束时：它就是一条普通新消息，靠下面贴的上下文说明与前文关联。
        # 这类迟到碎片通常很短（「吗」），会被 ④ 当噪音丢掉，所以先打个标记放行。
        late_frag = bool(
            frag_like
            and not mentioned
            and (now - self._last_pass.get(frag_key, 0.0))
            <= self.cfg.fragment_link_seconds
        )
        if late_frag:
            event.set_extra("ga_frag_note", label)
        # 顺手清掉过期缓存（避免串到下一轮）
        for k in [k for k, (ts, _l) in self._frag_pending.items() if now - ts > 60]:
            self._frag_pending.pop(k, None)
        for k in [k for k, t in self._merge_until.items() if now > t + 60]:
            self._merge_until.pop(k, None)
        for k, (refs0, ts0) in list(self._last_image.items()):
            if now - ts0 > self.cfg.image_carry_seconds * 5:
                self._last_image.pop(k, None)
        for k in list(self._frag_mentioned):
            if now > self._merge_until.get(k, 0.0) + 60:
                self._frag_mentioned.pop(k, None)

        # ④ 无内容消息（图片 / 表情包 / 单个标点 / 单字）—— **从群友名字入手**判断该不该回。
        #    这类消息没有文字语义，判不了它跟话题有没有关系；唯一确定性的线索是**名字**：
        #      · 这条本身就点着它的名 / 引用的就是它
        #      · 这个人最近（name_link_seconds 内）@ 过它，或在发言里叫过它的昵称
        #      · 机器人刚在跟这个人说话（_last_bot_talk，回复真正发出时记的）
        #    名字对得上 = 是接着跟它说（典型：@ 它之后紧接着甩一张图）→ 放行给 LLM；
        #    对不上 = 与上下文无关 → 只记 L1、不回（2026-10-09 用户要求）。
        #    被 @ 的不受影响（那是明确要它答）；迟到碎片也不受影响（见上）。
        if (
            not mentioned
            and not late_frag
            and (
                _is_contentless(label)
                or (self.cfg.skip_noise_messages and _is_noise(label))
            )
        ):
            if self.cfg.skip_contentless_messages and not await self._linked_to_bot(
                event, gid, sender, label, now
            ):
                logger.info(
                    "[group_activation] 群 %s 无内容消息、名字对不上（没人点它、它也没在跟"
                    "这个人说话）→ 只记 L1、不回：%r",
                    gid,
                    label[:20],
                )
                return
            logger.info(
                "[group_activation] 群 %s 无内容消息，但名字对得上（刚点过它 / 它刚在跟"
                "他说话）→ 放行：%r",
                gid,
                label[:20],
            )

        # ⑤ 放行给默认 LLM —— 这就是"全部按默认处理"
        event.is_at_or_wake_command = True
        # 被 @ 的那条：必须回；窗口内的普通消息：让 LLM 有权用 [PASS] 表示不参与
        event.set_extra("ga_mentioned", mentioned)
        if not mentioned:
            event.set_extra("ga_decidable", True)
        # 碎片合并（与 ③' 配对）：记下放行时刻；若这条"像还没说完"，
        # 就让 on_llm_request 先等 merge_seconds 收续句，再合并成一句话生成。
        self._last_pass[frag_key] = now
        # 开「等续句」窗口的两种消息：
        #   · 像碎片的短句（「这是你」→ 等「吗」）
        #   · 无内容消息（图片/表情包/标点）—— 「先发一张图，再问一句这是什么」
        #     是同一个动作，后一句必须并进这条（图只有这条事件里才有！）
        if frag_like or _is_contentless(label):
            event.set_extra("ga_frag_key", frag_key)
            self._merge_until[frag_key] = now + self.cfg.merge_seconds
        logger.info(
            "[group_activation] 群 %s 放行默认 LLM（被@=%s 剩余=%.0fs）msg=%r",
            gid,
            mentioned,
            self.wm.remaining(gid, now),
            label[:60],
        )

        # ④' KB 检索预取。检索要花 ~1s（本机 bge-m3 嵌入），如果等 on_llm_request
        #    才开始查，这段时间就**串行**加在回复延迟上（实测 0.8~1.8s，用户可感知）。
        #    消息一到就后台开跑，到真正要注入时大概率已经查完 —— 完全藏进管线时间。
        #    只在确定要放行后启动（②③ 早退的消息用不上，白烧一次嵌入）。
        if self.cfg.kb_enable:
            q = (text or "").strip()
            if q:
                old = self._kb_prefetch.get(gid)
                if old and old[0] != q and not old[1].done():
                    old[1].cancel()
                self._kb_prefetch[gid] = (q, asyncio.create_task(self.kbs.search(gid, q)))

        # ⑤ L3 长期记忆：挂一个「窗口休眠后提炼」的延迟任务（完全不占回复延迟）
        self._schedule_l3(gid)

    async def _is_repeat(self, gid: str, text: str) -> bool:
        """本条回复是否与自己**上一条**回复高度相似（自算余弦、带缓存）。

        阈值 0.90 与 L3/L4 同一套。比的是整段回复文本，只有「几乎在说同一件事」
        才会命中 —— 换个角度的补充、正常的跟进追问都不会被误伤。
        嵌入调用失败一律放行（宁可多发一条，不因故障吞消息）。
        """
        prev = self._last_reply.get(gid or "")
        t = (text or "").strip()
        if not prev or not t:
            return False
        try:
            score = await self.kbs.similarity(t, prev)
        except Exception as e:  # noqa: BLE001
            logger.warning("[group_activation] 复读检测失败: %s", e)
            return False
        if score >= self.cfg.reply_dedup_score:
            logger.info(
                "[group_activation] 复读检测命中（群 %s，相似度 %.2f）：%r ≈ 上一条 %r",
                gid,
                score,
                t[:30],
                prev[:30],
            )
            return True
        return False

    async def _kb_search_for(self, gid: str, query: str) -> str:
        """带预取的知识库检索。

        on_group_message 阶段就会为这条消息启动后台检索（见 ④'），
        到注入时大概率已完成 —— 检索的 ~1s 藏进管线时间里。
        预取不存在 / 对不上号（消息排队、被合并等）就当场查，行为不变；
        对不上号的旧任务直接取消，不白烧嵌入。
        """
        entry = self._kb_prefetch.pop(gid, None)
        if entry is not None:
            q, task = entry
            if q == query:
                try:
                    return await asyncio.wait_for(task, timeout=5)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[group_activation] KB 预取未命中，改当场查（群 %s）: %s",
                        gid,
                        e,
                    )
            elif not task.done():
                task.cancel()
        return await self.kbs.search(gid, query)

    def _known_members(self, gid: str) -> list[str]:
        """本群「出现过的人」= L3 记下的群员 + L1 里发过言的人。

        L3 排前面：长期见过的老成员优先，注入条数有限时才不会被
        刚冒泡一次的人挤掉。机器人自己的消息不进 L1、也不进 L3，所以不用额外排除。
        """
        names: list[str] = []
        try:
            names.extend(str(k) for k in self.ltm.members(gid))
        except Exception as e:  # noqa: BLE001
            logger.debug("[group_activation] 取 L3 成员失败: %s", e)
        try:
            names.extend(str(it.get("user") or "") for it in self.wm.state(gid).l1)
        except Exception as e:  # noqa: BLE001
            logger.debug("[group_activation] 取 L1 成员失败: %s", e)

        out: list[str] = []
        for n in names:
            n = n.strip()
            if n and n != "unknown" and n not in out:
                out.append(n)
        return out

    @staticmethod
    def _describe_components(event: AstrMessageEvent) -> str:
        """把非文本组件转成占位符串（图片/语音/视频/文件）。

        只用于**记忆**；消息本身是否放行给默认 LLM 由别处的规则决定。
        图片的真实内容由 AstrBot 直接送给多模态模型，这里只是让 L1 背景
        不出现「[空]」这种毫无信息量的记录。
        """
        try:
            chain = getattr(event.message_obj, "message", None) or []
        except Exception:  # noqa: BLE001
            return ""
        marks: list[str] = []
        for comp in chain:
            raw = getattr(comp, "type", "")
            name = str(getattr(raw, "value", raw)).lower()
            if "image" in name:
                marks.append("[图片]")
            elif "record" in name or "audio" in name:
                marks.append("[语音]")
            elif "video" in name:
                marks.append("[视频]")
            elif "file" in name:
                marks.append("[文件]")
        return " ".join(marks)

    # ── 1.5 「从名字入手」判定：无内容消息该不该回 ─────────────
    async def _bot_names_of(self, event: AstrMessageEvent) -> set[str]:
        """机器人自己的名字（微信昵称），供名字判定用。

        昵称从桥的 OneBot `get_login_info` 取（桥返回 `wx.self_name`，即登录的
        微信昵称）；配置 `bot_names` 可再补别名/错字。取不到也不致命 ——
        「这个人最近 @ 过它」这条线索靠的是 L1 的 `at` 标记，与名字无关。
        """
        if self._bot_names:
            return self._bot_names
        if time.time() - self._bot_name_try < 60:      # 取失败后 60 秒内不重试
            return self._bot_names
        self._bot_name_try = time.time()
        names = {str(n).strip() for n in (self.cfg.bot_names or []) if str(n).strip()}
        try:
            call = getattr(getattr(event, "bot", None), "call_action", None)
            if call is not None:
                info = await call("get_login_info")
                nick = ""
                if isinstance(info, dict):
                    nick = str(info.get("nickname") or "")
                elif info is not None:
                    nick = str(getattr(info, "nickname", "") or "")
                nick = nick.strip()
                if nick:
                    names.add(nick)
                    logger.info(
                        "[group_activation] 机器人昵称：%r（无内容消息判定用）", nick
                    )
        except Exception as e:  # noqa: BLE001
            logger.debug("[group_activation] 取机器人昵称失败：%s", e)
        self._bot_names = names
        return names

    async def _linked_to_bot(
        self, event: AstrMessageEvent, gid: str, sender: str, label: str, now: float
    ) -> bool:
        """「无内容消息」（图片/表情包/标点）是否接着它在说 —— **只看名字**。

        为什么只看名字：这类消息没有文字语义，判不了话题相关性；名字是唯一确定性线索。
        名字对得上的三种情况（任一成立即可回）：
          1. 这条消息本身点着它的名 / 引用的就是它（桥的引用前缀「（引用 <昵称> 的消息…）」）
          2. 这个人最近 @ 过它，或在发言里叫过它的昵称（翻 L1，只看同一个人的行）
          3. 机器人刚在跟这个人说话（`_last_bot_talk`，回复真正发出时记的）
        """
        names = await self._bot_names_of(event)

        # 1. 本条消息里点它的名 / 引用它
        for n in names:
            if n and (n in label or f"引用 {n}" in label):
                return True

        # 3. 机器人刚在跟这个人说话 → 他接着发个表情包/图，是给它的
        talk = self._last_bot_talk.get(gid)
        if (
            talk
            and talk[0] == sender
            and now - talk[1] <= self.cfg.name_link_seconds
        ):
            return True

        # 2. 这个人最近点过它 / 叫过它的名（L1 里同一发送者的行）
        for it in reversed(list(self.wm.state(gid).l1)):
            if now - float(it.get("ts") or 0.0) > self.cfg.name_link_seconds:
                break
            if str(it.get("user") or "") != sender:
                continue
            if it.get("at"):
                return True
            txt = str(it.get("text") or "")
            if any(n and n in txt for n in names):
                return True
        return False

    @staticmethod
    def _image_refs(event: AstrMessageEvent) -> list[str]:
        """取本条消息里图片的可用引用（`base64://` / `http(s)://` / `file://` / 本地路径）。

        用途见 inject_memory 的「图片携带」：先发图、再问一句「这是什么」时，
        把图补挂到后一条的请求上 —— 图片**只在它自己那条事件里**才会送给模型，
        后一条纯文字消息里没有它，模型只能看到 L1 里的 `[图片]` 占位符。
        """
        refs: list[str] = []
        try:
            chain = getattr(event.message_obj, "message", None) or []
        except Exception:  # noqa: BLE001
            return refs
        for comp in chain:
            raw = getattr(comp, "type", "")
            name = str(getattr(raw, "value", raw)).lower()
            if "image" not in name:
                continue
            for attr in ("url", "file", "path"):
                val = getattr(comp, attr, None)
                if isinstance(val, str) and val.strip():
                    refs.append(val.strip())
                    break
        return refs

    @staticmethod
    def _wake_by_components(event: AstrMessageEvent) -> str:
        """按**消息段**自查是不是在叫它；返回命中的原因（"" = 没命中）。

        覆盖框架 `WakingCheckStage` 的同一套判定：@ 它本人 / @ 全体 / 引用它的消息。
        为什么要自己再判一遍：实测单发一个「@它」（无正文）时框架没有置位
        `is_at_or_wake_command`（2026-10-09），插件于是沉默处理 —— 表现为
        「@ 了它，它却看不见你上一条」。
        """
        self_id = str(event.get_self_id() or "")
        try:
            chain = getattr(event.message_obj, "message", None) or []
        except Exception:  # noqa: BLE001
            return ""
        for comp in chain:
            raw = getattr(comp, "type", "")
            name = str(getattr(raw, "value", raw)).lower()
            if name == "at":
                q = str(getattr(comp, "qq", "") or "")
                if q == "all":
                    return "at_all"
                if self_id and q == self_id:
                    return "at_me"
            elif name == "reply":
                sid = str(getattr(comp, "sender_id", "") or "")
                if self_id and sid == self_id:
                    return "reply_me"
        return ""

    def _recent_speech(self, gid: str, sender: str, now: float) -> str:
        """找「同一发送者最近说过的、值得回应的一句」（供空 @ 接上下文用）。

        跳过：纯占位符（[图片]/[表情包]…）、无内容（纯标点）、沉默标记、空文本。
        """
        for it in reversed(list(self.wm.state(gid).l1)):
            if now - float(it.get("ts") or 0.0) > self.cfg.bare_at_lookback_seconds:
                break
            if str(it.get("user") or "") != sender:
                continue
            t = str(it.get("text") or "").strip()
            if (
                not t
                or t == self.cfg.pass_marker.strip()
                or t in _NOISE_PLACEHOLDERS
                or _is_contentless(t)
            ):
                continue
            return t
        return ""

    # ── 2. 记忆注入 ───────────────────────────────────
    @filter.on_llm_request()
    async def inject_memory(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """向默认 LLM 注入三样：L1 群聊背景、L3 长期记忆、「可以不说」的参与规则。"""
        gid = event.get_group_id()
        if not gid:
            return
        if not self.wm.is_active(gid):
            return

        parts: list[str] = []

        if self.cfg.inject_l1:
            block = self.wm.render_l1(gid, exclude_last=True)
            if block:
                parts.append(block)
                # 背景里若有「无内容」行（图片/表情包/纯标点），补一句说明：
                # 它们只是背景，没人点它的名时不要去回应 —— 否则模型会挑着这些行答，
                # 群里看到的就是「对着一个表情包/一串标点自说自话」。
                if any(
                    _is_contentless(line.split("：", 1)[-1].split("←", 1)[0])
                    for line in block.splitlines()
                    if line.startswith("「")
                ):
                    parts.append(
                        "（上面内容为 `[图片]`/`[表情包]` 或纯标点的行没有文字内容，"
                        "只是背景 —— 没有人点你的名时，不要去回应它们。）"
                    )
                logger.info(
                    "[group_activation] 已注入 L1 背景（群 %s，%d 条）",
                    gid,
                    block.count("\n"),
                )

        # L3：只注入**本群**的长期记忆（按群天然隔离）
        if self.cfg.l3_enable:
            mem = self.ltm.render(gid)
            if mem:
                parts.append(mem)
                logger.info(
                    "[group_activation] 已注入 L3 长期记忆（群 %s，%d 行）",
                    gid,
                    mem.count("\n"),
                )

        # L4：本群公共知识库 —— 用当前消息做语义检索，只带相关的几条。
        # 检索在 on_group_message 阶段已经预取（见 ④'），这里通常只是取结果。
        # 检索失败绝不影响回复（知识库是锦上添花，不是必需品）。
        if self.cfg.kb_enable:
            query = (event.message_str or "").strip()
            try:
                kb_block = await self._kb_search_for(gid, query)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[group_activation] 知识库检索失败（群 %s）: %s", gid, e
                )
                kb_block = ""
            if kb_block:
                parts.append(f"{self.cfg.kb_header}\n{kb_block}")
                logger.info(
                    "[group_activation] 已注入知识库检索（群 %s，%d 行）",
                    gid,
                    kb_block.count("\n"),
                )

        # 指名回复：把「本群出现过的人」交给模型，并约定 @ 的写法。
        # 桥会在出站正文里把 `@昵称` 换成微信真实 @（只认本群真实成员）。
        if self.cfg.enable_mention:
            names = self._known_members(gid)
            if names:
                parts.append(
                    self.cfg.mention_prompt.replace(
                        "{names}", "、".join(names[: self.cfg.mention_max_names])
                    )
                )
                # 本条消息**是谁发的**：无条件告知，不只被 @ 时。
                # 为什么必须无条件：AstrBot 丢给模型的那条 user 消息里**只有正文、
                # 没有发送者**，窗口内的消息原来完全不带人名，模型只能靠猜 ——
                # 实测就会「认错人」（把 A 说的话当成 B 说的，2026-10-08）。
                # 这一行贴着眼前这条消息走，是最可靠的归属信息。
                sender = (event.get_sender_name() or "").strip()
                if sender:
                    # 措辞注意（条件式）：写成「回的时候…」会暗示它「该回话」，
                    # 与「默认沉默」相冲 —— 实测会明显变话痨。
                    line = (
                        f"（本条消息是「{sender}」发的；若决定接话，"
                        "别把说话的人搞混。）"
                    )
                    if event.get_extra("ga_mentioned", False) and sender in names:
                        # 被 @ 的那条：**必须 @ 回去**（对方要收到提醒）。
                        # 之前写的是「若只想简短回他这一两句…回答较长时不要 @」，
                        # 结果模型长期不 @，用户被 @ 了却收不到回敬提醒。
                        line += (
                            f"这条消息 @ 了你 → 回复**开头**写 `@{sender}` 当称呼。"
                        )
                    parts.append(line)
                logger.info(
                    "[group_activation] 已注入指名回复说明（群 %s，名单 %d 人：%s）",
                    gid,
                    len(names),
                    "、".join(names[:5]),
                )

        # ③' 碎片合并（与 on_group_message ③' 配对）：先等 merge_seconds，
        #     把对方紧接着发来的短句收进来，明确告诉模型「这是一句话」——
        #     这样它不会先答「这是你」再答「吗」，只出一条回复。
        frag_key = event.get_extra("ga_frag_key")
        if frag_key:
            await asyncio.sleep(self.cfg.merge_seconds)
            # 等待结束：此后到来的短句不再并入本条（见 on_group_message ③'）
            self._merge_until.pop(frag_key, None)
            # 等这段时间里，对方还可能发过「@ 你」的那句 —— 它被吞进本条了，
            # 所以本条必须回、并且开头 @ 回去（否则 @ 的人收不到提醒）。
            who = self._frag_mentioned.pop(frag_key, "")
            if who:
                event.set_extra("ga_mentioned", True)
                parts.append(
                    f"（对方连着发的消息里有一条 @ 了你（{who}）→ "
                    f"回复**开头**写 `@{who}` 当称呼。）"
                )
            ts, extra = self._frag_pending.pop(frag_key, (0.0, []))
            if extra and (time.time() - ts) < self.cfg.merge_seconds * 4:
                here = (event.message_str or "").strip()
                merged = "".join([here, *extra])
                parts.append(
                    "（对方是连着发的几条：「"
                    + "」「".join([here, *extra])
                    + f"」—— 合起来是同一句话「{merged}」，"
                    "按一句话理解，**只回一条**，别分开答。）"
                )
                logger.info(
                    "[group_activation] 合并碎片（%s）：%r + %r → %r",
                    frag_key,
                    here,
                    extra,
                    merged[:60],
                )

        # ③'' 图片携带：对方**先发了一张图**，本条是随后的纯文字（「这是什么」）。
        #      图片只在它自己那条事件里会送给模型 —— 本条没有图，模型只看得到 L1 里的
        #      `[图片]` 占位符，只能答「我看不到图」。这里把图补挂到本请求上。
        #      有效性：本钩子跑在框架 image_input 之前（internal.py: OnLLMRequestEvent →
        #      prepare_request_images），所以写 req.image_urls 会被真正处理成输入。
        #      只补挂一次（补完就出队），避免这个人的每条消息都重复付费上图。
        if not self._image_refs(event):
            img_key = f"{gid}:{event.get_sender_id() or ''}"
            refs, its = self._last_image.get(img_key, ([], 0.0))
            if refs and time.time() - its <= self.cfg.image_carry_seconds:
                self._last_image.pop(img_key, None)
                cur = list(getattr(req, "image_urls", None) or [])
                add = [r for r in refs if r not in cur]
                if add:
                    req.image_urls = cur + add
                    parts.append(
                        "（对方刚发过一张图，已随本条一起给出 —— 他要问的多半就是这张图。）"
                    )
                    logger.info(
                        "[group_activation] 已把上一条的图补挂到本条请求（群 %s，%d 张）",
                        gid,
                        len(add),
                    )

        # 空 @：这条只是 @ 了你、没有正文 —— 明确告诉他"要答的是他刚说的那句"，
        # 否则模型只会回一句「本鱼在」（2026-10-09 实况）。
        bare_ctx = event.get_extra("ga_bare_at_ctx")
        if bare_ctx:
            parts.append(
                f"（这条消息只是 @ 了你、**没有正文** —— 他刚说过的、还没被你接的是："
                f"「{bare_ctx}」。他这一下是在叫你接着答那句，**直接回答那句就行**，"
                "不要说「你没说话」或反问他想问什么。）"
            )

        # 迟到碎片：上一轮已经在生成（或已发出）时，对方又敲了一句短话。
        # 明确告诉模型「这多半是同一句话的后半截」，否则它会把它当成一句孤立的新话
        # 另起一轮 —— 用户看到的就是「先答『这是你』、再答『吗』」两条不相关的回复。
        late = event.get_extra("ga_frag_note")
        if late:
            parts.append(
                f"（对方刚刚连着发了几条，这条「{late}」很可能是在把同一句话分开打 ——"
                " 结合上面 L1 里他上一条刚说的话，理解成**同一句话**；"
                "若上一条已经答了前半句，就直接接着答后半句，别当成一句孤立的新话。）"
            )

        # 只有「未被 @ 的窗口内消息」才允许不发言；被 @ 的那条必须回
        if self.cfg.llm_decides_participation and not event.get_extra(
            "ga_mentioned", False
        ):
            parts.append(
                self.cfg.participation_prompt.replace("{marker}", self.cfg.pass_marker)
            )

        if parts:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + "\n\n".join(parts)

    # ── 2.5 @ 校正：回复里的 @ 只允许指向本条消息的发送者 ─────
    @filter.on_decorating_result()
    async def fix_mentions(self, event: AstrMessageEvent) -> None:
        """把回复正文里的 @ 校正为「只能 @ 本条消息的发送者」。

        为什么必须硬校正（提示词管不住）：模型有**自我模仿**惯性 —— 它的历史
        回复里攒了一堆「@如月之影 」，之后不管谁问都照抄这个开头，实测连对方
        说「艾特我」的时候都能 @ 错人（2026-10-08 实况）。这和 [PASS] 事故同源：
        坏输出进了 L2，被反复模仿。提示词只能影响单次生成，历史模仿是长期项，
        所以用确定性规则兜底（与「@ 别人就不参与」同一思路）：

        - 模型 @ 的就是发送者 → 放行
        - 本条消息里**点名了**这个目标（「atNULL」「艾特小泽布尔」）→ 放行：
          这是对方明确要求 @ 谁，不是乱 @（上一版没这个例外，把「atNULL」
          的 `@NULL` 强行改成了 `@小泽布尔` —— 矫枉过正，已修）
        - 其余情况（消息里根本没提这个人）→ 改成 @ 发送者：谁问的就 @ 谁
        - 同一回复里第 2 个及以后的 @ → 直接去掉（一条消息最多一个 @）
        """
        if not self.cfg.enable_mention or not self.cfg.mention_only_sender:
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        gid = event.get_group_id()
        sender = (event.get_sender_name() or "").strip()
        if not gid or not sender:
            return
        targets = sorted(
            (n for n in self._known_members(gid) if n), key=len, reverse=True
        )
        if not targets:
            return
        pat = re.compile("@" + "(?:" + "|".join(re.escape(n) for n in targets) + ")")
        seen_first = False
        # 本条消息的原文：对方点名要求 @ 谁时（「atNULL」），那个目标是合法的。
        # 大小写不敏感 —— 群里常写小写（atnull / asuka）。
        incoming_low = (event.message_str or "").lower()

        def _named_in_message(name: str) -> bool:
            n = name.lower()
            if not n:
                return False
            # 中文名 / 较长名字：子串命中即可
            if len(n) >= 4 or not n.isascii():
                return n in incoming_low
            # 短的纯英文名（li / aki…）：要求词边界 —— 否则「发个 link」里的
            # "li" 也会被当成点名，把乱 @ 放行掉
            return (
                re.search(
                    r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", incoming_low
                )
                is not None
            )

        def _sub(m: re.Match) -> str:
            nonlocal seen_first
            name = m.group(0)[1:]
            if not seen_first:
                seen_first = True
                if name == sender:
                    return m.group(0)
                if _named_in_message(name):
                    logger.info(
                        "[group_activation] 放行 @%s（群 %s）：本条消息点名要他",
                        name,
                        gid,
                    )
                    return m.group(0)
                logger.info(
                    "[group_activation] 校正错 @（群 %s）：模型写 @%s，"
                    "但本条是 %s 发的、消息里也没点名 %s → 改成 @%s",
                    gid,
                    name,
                    sender,
                    name,
                    sender,
                )
                return "@" + sender
            logger.info(
                "[group_activation] 去掉多余 @%s（群 %s，一条消息只留一个 @）",
                name,
                gid,
            )
            return ""

        for comp in result.chain:
            raw = getattr(comp, "text", None)
            if not isinstance(raw, str) or "@" not in raw:
                continue
            new = pat.sub(_sub, raw)
            new = re.sub(r"[ \t]{2,}", " ", new)
            if new != raw:
                comp.text = new

        # 兜底补 @：被 @ 的那条回复，如果模型一个 @ 都没写，就自动补上发送者 ——
        # 保证「@ 我 → 它回我」时对方能收到微信提醒。
        # 为什么用确定性规则而不是提示词：mention_prompt 里原本写着「默认不用、少用」
        # 和「回答较长时不要 @」，模型于是长期一个 @ 都不写，用户被 @ 了却没有回敬提醒
        # （2026-10-09 实况：连发三次要它 @，它只成功了一次）。
        if event.get_extra("ga_mentioned", False) and not seen_first:
            for comp in result.chain:
                raw = getattr(comp, "text", None)
                if isinstance(raw, str) and raw.strip():
                    comp.text = f"@{sender} {raw.lstrip()}"
                    logger.info(
                        "[group_activation] 回复未带 @，自动补 @%s（群 %s，被 @ 的那条）",
                        sender,
                        gid,
                    )
                    break

    # ── 3. LLM 自主决定是否接话 ──────────────────────
    @filter.on_decorating_result()
    async def decide_participation(self, event: AstrMessageEvent) -> None:
        """LLM 有权「不发言」：输出恰为约定标记时清空结果，消息不发送。

        只对窗口内「未被 @ 的」消息生效 —— 被 @ 的那条永远发送。
        这样不需要额外一次 LLM 往返，**不增加任何延迟**；
        若改成「插件先调一次 LLM 判定」，每次接话都会多约 1.2 秒。
        """
        if not self.cfg.llm_decides_participation:
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        text = (result.get_plain_text() or "").strip().strip("。.！!？?~～ \t\r\n")
        decidable = bool(event.get_extra("ga_decidable", False))
        # 诊断：证明本钩子确实被调用（否则「沉默」机制可能只是死代码）
        logger.info(
            "[group_activation] 参与判定已生效（群 %s，可沉默=%s）：模型输出 %r",
            event.get_group_id(),
            decidable,
            (result.get_plain_text() or "")[:30],
        )
        if text != self.cfg.pass_marker.strip():
            # 防复读闸门：**窗口内（可沉默的）**消息，如果这条回复与自己上一条
            # 高度相似，直接不发 —— 实况（2026-10-08）：两人前后脚问同一张图，
            # 模型把「认不出来」讲了第二遍，群里看到的就是「自说自话」。
            # 被 @ 的那条必须回、不受此限（它靠提示词约束「别复读」）；
            # 代价：窗口内回复 +约 0.4s（一次本地嵌入），可用 reply_dedup_enable 关。
            gid2 = event.get_group_id()
            plain = result.get_plain_text() or ""
            if (
                decidable
                and self.cfg.reply_dedup_enable
                and await self._is_repeat(gid2, plain)
            ):
                logger.info(
                    "[group_activation] 复读拦下（群 %s）：本条与自己上一条太像，"
                    "不重复发",
                    gid2,
                )
                event.clear_result()
                return
            # 记住这条回复，供下一条比对（@ 的那条也记，后续窗口消息才能对上）
            if gid2 and plain.strip():
                self._last_reply[gid2] = plain.strip()[:500]
            return
        if decidable:
            logger.info(
                "[group_activation] LLM 选择不接话（群 %s），已取消本次回复",
                event.get_group_id(),
            )
        else:
            # 被 @ 的那条**本该必回**，模型却吐出了沉默标记 —— 它多半是在
            # L1/L2 里见过这个标记在照抄（见 on_group_message 里的说明）。
            # 无论如何**绝不能把字面量发进群**：那会再进 L1、被反复模仿。
            logger.warning(
                "[group_activation] 群 %s：被 @ 的消息里模型输出了沉默标记 %r，"
                "已拦下（不会发到群里）",
                event.get_group_id(),
                self.cfg.pass_marker,
            )
        event.clear_result()

    # ── 4. 机器人回复完成后，把窗口再撑一下 ──────────
    @filter.after_message_sent()
    async def extend_after_reply(self, event: AstrMessageEvent) -> None:
        """机器人回复完成后续期窗口。

        为什么必须有这一步：机器人从收到消息到真正发出回复，要经过
        「LLM 生成（约 1.2s）+ 界面键入与发送闸门（约 2–5s）」。
        这段时间若算在窗口内，用户看到回复再去打字时窗口往往已到期，
        表现为「@ 完再说话它不理」。
        把「回复完成」也当作一个活动点续期，用户才能拿到完整的
        idle_seconds 来回应。
        """
        gid = event.get_group_id()
        if not gid:
            return
        # 记下「机器人刚在跟谁说话」—— 供无内容消息的名字判定（_linked_to_bot）：
        # 这个人接着甩个图片/表情包/标点，那是**给它的**，名字对得上就该回；
        # 换了别人、或这个人没在跟它对话 → 不回（2026-10-09 用户要求）。
        talk_sender = (event.get_sender_name() or "").strip()
        if talk_sender:
            self._last_bot_talk[gid] = (talk_sender, time.time())
        # 无论窗口当前开没开，都把「回复完成」当作一个活动点：
        #   · 还开着 → 续期（touch）
        #   · 已过期 → **重新打开**（activate）
        # 为什么必须这样：长回复的生成可能要 30 秒以上（KMP 长代码答案实测 34s），
        # 窗口在生成期间就过期了；如果按「过期就跳过」处理，回复落进群里之后的
        # 黄金 30 秒反而是关着的 —— 实测「太难了，一点一点拆开来」就是这么被漏
        # 掉的（2026-10-08：答案 23:32:47 落下，23:33:21 的接话窗口已关）。
        if self.wm.is_active(gid):
            self.wm.touch(gid)
        else:
            if self.wm.active_count() >= self.cfg.active_groups_max:
                logger.info(
                    "[group_activation] 群 %s 回复后想重开窗口，但并发已满 %d",
                    gid,
                    self.cfg.active_groups_max,
                )
                return
            self.wm.activate(gid)
            logger.info(
                "[group_activation] 群 %s 窗口在生成期间已过期 → 回复后重新打开",
                gid,
            )
        logger.info(
            "[group_activation] 机器人回复后窗口续期（群 %s，剩余 %.0fs）",
            gid,
            self.wm.remaining(gid),
        )

    # ── 5. L3 长期记忆：窗口休眠后异步提炼 ─────────────
    def _schedule_l3(self, gid: str) -> None:
        """每次窗口活动都重挂一次「休眠后提炼」的延迟任务。

        为什么用延迟任务而不是等窗口 sleep：窗口是**懒判定**的（只在有消息进来时
        才检查是否过期），群里没人说话就永远等不到下一次检查。延迟
        `idle_seconds + 2` 秒后再回看一眼，既能及时提炼，又不占用正常回复的延迟。
        """
        if not self.cfg.l3_enable:
            return
        self._l3_seen[gid] = self._l3_seen.get(gid, 0) + 1
        old = self._l3_tasks.get(gid)
        if old and not old.done():
            old.cancel()
        self._l3_tasks[gid] = asyncio.create_task(self._l3_extract_later(gid))

    async def _l3_extract_later(self, gid: str) -> None:
        try:
            await asyncio.sleep(self.cfg.idle_seconds + 2)
            if self.wm.is_active(gid):
                return  # 窗口又被续上了，等下一轮
            await self._l3_extract(gid)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("[group_activation] L3 提炼异常（群 %s）: %s", gid, e)

    async def _l3_extract(self, gid: str) -> None:
        """窗口休眠后从 L1 提炼：群员事实（→L3）+ 群公共知识（→L4 知识库）。

        三道闸门：新消息数、冷却时间、有无内容。两路产出共享**同一次**
        LLM 调用（kb_extract_appendix 拼进同一个 prompt），不额外花钱。
        """
        seen = self._l3_seen.get(gid, 0)
        if seen < self.cfg.l3_min_new_messages:
            return
        now = time.time()
        if now - self._l3_last.get(gid, 0.0) < self.cfg.l3_cooldown_seconds:
            return

        st = self.wm.state(gid)
        lines = [f"{it['user']}: {it['text']}" for it in st.l1]
        if not lines:
            return

        provider = await self.context.get_using_provider_async()
        if not provider:
            logger.warning("[group_activation] L3 提炼跳过：没有可用的对话模型")
            return

        resp = await provider.text_chat(
            prompt=self._build_extract_prompt() + "\n".join(lines),
            system_prompt="你是一个只输出 JSON 的信息抽取器，不要输出任何解释。",
        )
        raw = (getattr(resp, "completion_text", "") or "").strip()
        if not raw and getattr(resp, "result_chain", None):
            raw = resp.result_chain.get_plain_text().strip()

        facts, knowledge = self._parse_extraction(raw)
        self._l3_last[gid] = now
        self._l3_seen[gid] = 0
        if not facts and not knowledge:
            logger.info("[group_activation] L3 提炼完成（群 %s）：无新内容", gid)
            return

        by_member: dict[str, list[str]] = {}
        for f in facts:
            by_member.setdefault(f["member"], []).append(f["fact"])
        added = 0
        for member, items in by_member.items():
            kept = await self._dedup_l3_facts(gid, member, items)
            added += self.ltm.add_facts(gid, member, kept)
        logger.info(
            "[group_activation] L3 提炼完成（群 %s）：提取 %d 条，新增 %d 条，累计 %d 条",
            gid,
            len(facts),
            added,
            self.ltm.total_facts(gid),
        )

        # L4：群公共知识 → AstrBot 知识库（语义查重 + 每日额度，见 kb.py）
        if knowledge:
            try:
                n, detail = await self.kbs.remember(gid, knowledge)
                logger.info(
                    "[group_activation] 知识库提炼结果（群 %s）：%s", gid, detail
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[group_activation] 知识库写入异常（群 %s）: %s", gid, e
                )

    async def _dedup_l3_facts(
        self, gid: str, member: str, items: list[str]
    ) -> list[str]:
        """L3 语义去重：与**本人已有事实**相似度 ≥ 阈值的跳过（同批内也互相去重）。

        为什么需要：`add_facts` 只按**文本精确匹配**去重，换个说法就会重复入库
        —— 实测小泽布尔 11 条里并存「会自己搓工具并上传GitHub」「会写代码，愿意
        分享到GitHub」。相似度用和 L4 同一套自算余弦（带缓存，失败返回 0 即放行
        —— 宁可多记一条，不因嵌入故障丢信息）。
        """
        if not self.cfg.l3_dedup_enable or not items:
            return items
        mem = self.ltm.members(gid).get(member)
        existing: list[str] = [f.text for f in (mem.facts if mem else [])]
        kept: list[str] = []
        for it in items:
            dup_of = ""
            for e in existing:
                try:
                    score = await self.kbs.similarity(it, e)
                except Exception as e2:  # noqa: BLE001
                    logger.warning("[group_activation] L3 去重相似度计算失败: %s", e2)
                    score = 0.0
                if score >= self.cfg.l3_dedup_score:
                    dup_of = e
                    break
            if dup_of:
                logger.info(
                    "[group_activation] L3 去重跳过（群 %s，%s）：%r ≈ 已有 %r",
                    gid,
                    member,
                    it,
                    dup_of,
                )
                continue
            kept.append(it)
            existing.append(it)  # 同批新增的也参与后续去重
        return kept

    def _build_extract_prompt(self) -> str:
        """L3 提炼 prompt + L4 追加任务。

        追加的内容要插在「# 群聊记录」小节**之前**（那后面跟的是聊天记录），
        而配置里的 l3_extract_prompt 可能被用户改过，所以按标记动态定位。
        """
        prompt = self.cfg.l3_extract_prompt
        marker = "# 群聊记录"
        if marker in prompt:
            head, _, tail = prompt.partition(marker)
            return head + self.cfg.kb_extract_appendix + marker + tail
        return prompt + self.cfg.kb_extract_appendix

    @staticmethod
    def _parse_extraction(raw: str) -> tuple[list[dict], list[str]]:
        """从模型输出中稳健地抠出 facts 与 knowledge（容忍代码块标记与多余文字）。"""
        if not raw:
            return [], []
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return [], []
        try:
            data = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return [], []
        out: list[dict] = []
        for item in data.get("facts") or []:
            if not isinstance(item, dict):
                continue
            member = str(item.get("member") or "").strip()
            fact = str(item.get("fact") or "").strip()
            if member and fact:
                out.append({"member": member, "fact": fact[:40]})
        knowledge = [
            str(k).strip()[:60] for k in (data.get("knowledge") or []) if str(k).strip()
        ]
        return out, knowledge

    # ── 6. 联网搜索（百度 AI 搜索，自管每日额度）────────
    def _search_usage_path(self):
        """额度计数文件：与 L3 长期记忆放在同一个插件数据目录。"""
        return self.ltm.path.parent / "search_usage.json"

    def _search_quota_left(self) -> int:
        """今天还能搜几次。跨天自动清零。"""
        today = time.strftime("%Y-%m-%d")
        try:
            with open(self._search_usage_path(), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            return self.cfg.search_daily_limit
        if data.get("date") != today:
            return self.cfg.search_daily_limit
        try:
            used = int(data.get("count", 0))
        except (TypeError, ValueError):
            used = 0
        return max(0, self.cfg.search_daily_limit - used)

    def _search_count_up(self) -> None:
        """搜索成功后计数 +1（原子写，避免写坏计数文件）。"""
        today = time.strftime("%Y-%m-%d")
        try:
            path = self._search_usage_path()
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:  # noqa: BLE001
                data = {}
            used = int(data.get("count", 0)) if data.get("date") == today else 0
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"date": today, "count": used + 1}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as e:  # noqa: BLE001
            logger.warning("[group_activation] 写搜索计数失败: %s", e)

    def _baidu_search_key(self) -> str:
        """百度 AI 搜索的 Key 存在 AstrBot 主配置的 provider_settings 里。"""
        try:
            ps = self.context.get_config().get("provider_settings") or {}
            return str(ps.get("websearch_baidu_app_builder_key") or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.debug("[group_activation] 读百度搜索 Key 失败: %s", e)
            return ""

    @filter.llm_tool(name="web_search")
    async def web_search(self, event: AstrMessageEvent, query: str) -> str:
        '''联网搜索（百度）。**仅当用户明确要求搜索、查网上资料、或让你上网查时才调用**；闲聊、用户没提搜索时绝对不要调用这个工具。

        Args:
            query(string): 搜索关键词，简短、具体
        '''
        if not self.cfg.search_enable:
            return "搜索功能未启用。"
        left = self._search_quota_left()
        if left <= 0:
            logger.info(
                "[group_activation] 联网搜索已达今日上限（%d 次），本次拒绝",
                self.cfg.search_daily_limit,
            )
            return (
                f"今日联网搜索次数已用完（上限 {self.cfg.search_daily_limit} 次），"
                "请直接告诉用户今天搜不了，明天再试。"
            )
        key = self._baidu_search_key()
        if not key:
            logger.warning("[group_activation] 百度搜索 Key 未配置")
            return "搜索服务未配置 API Key。"

        import aiohttp

        payload = {
            "messages": [{"role": "user", "content": str(query)[:72]}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": self.cfg.search_top_k}],
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "X-Appbuilder-Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.post(
                    "https://qianfan.baidubce.com/v2/ai_search/web_search",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            "[group_activation] 百度搜索失败 %s: %s",
                            resp.status,
                            body[:200],
                        )
                        return f"搜索失败（HTTP {resp.status}），请直接告诉用户搜不了。"
                    data = await resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("[group_activation] 百度搜索异常: %s", e)
            return f"搜索失败：{e}"

        refs = [
            r
            for r in (data.get("references") or [])
            if isinstance(r, dict) and r.get("url")
        ]
        if not refs:
            logger.info("[group_activation] 百度搜索无结果：%r", str(query)[:40])
            return "搜索没有返回结果。"
        self._search_count_up()
        logger.info(
            "[group_activation] 联网搜索完成（query=%r，今日剩余 %d 次）",
            str(query)[:40],
            self._search_quota_left(),
        )
        lines = [
            f"{i}. {r.get('title', '')}\n   链接: {r.get('url', '')}\n   摘要: {str(r.get('content', ''))[:220]}"
            for i, r in enumerate(refs[: self.cfg.search_top_k], start=1)
        ]
        return "联网搜索结果（请基于这些内容回答，并注明来源）：\n" + "\n".join(lines)

    # ── 7. 调试命令 ──────────────────────────────────
    @filter.command("gaw")
    async def gaw(self, event: AstrMessageEvent) -> None:
        """查看当前激活窗口。"""
        snap = self.wm.snapshot()
        if not snap:
            await event.send(MessageChain().message("当前没有激活中的群窗口。"))
            return
        lines = [
            f"- {s['group_id']}: 剩余 {s['remaining']}s, L1={s['l1']}" for s in snap
        ]
        await event.send(
            MessageChain().message("激活中的群窗口：\n" + "\n".join(lines))
        )

    @filter.command("gactx")
    async def gactx(self, event: AstrMessageEvent) -> None:
        """查看本群将注入给 LLM 的记忆内容。"""
        gid = event.get_group_id()
        block = self.wm.render_l1(gid, exclude_last=False) if gid else ""
        if not block:
            await event.send(MessageChain().message("本群记忆为空。"))
            return
        await event.send(MessageChain().message("本群记忆：\n" + block))

    @filter.command("gasleep")
    async def gasleep(self, event: AstrMessageEvent) -> None:
        """强制让当前群休眠。"""
        gid = event.get_group_id()
        self.wm.sleep(gid)
        await event.send(MessageChain().message(f"群 {gid} 已强制休眠。"))

    @filter.command("gaclear")
    async def gaclear(self, event: AstrMessageEvent) -> None:
        """清空本群的 L1 记忆并休眠。

        用途：群里有人用自然语言临时改了人设（例如「你现在是猫娘」），
        那句话会被记进 L1 并持续影响后续回复。`/reset` 只能清 AstrBot
        原生会话（L2），清不掉 L1 —— 所以需要这条命令。
        """
        gid = event.get_group_id()
        if not gid:
            return
        st = self.wm.state(gid)
        n = len(st.l1)
        st.l1.clear()
        self.wm.sleep(gid)
        await event.send(
            MessageChain().message(
                f"已清空群 {gid} 的 L1 记忆（{n} 条）并休眠。\n"
                "如需同时清掉 AstrBot 原生会话（多轮历史），请再发一次 /reset。"
            )
        )

    @filter.command("gamem")
    async def gamem(self, event: AstrMessageEvent) -> None:
        """查看本群的长期记忆（L3）。"""
        gid = event.get_group_id()
        if not gid:
            return
        await event.send(MessageChain().message(self.ltm.dump(gid)))

    @filter.command("gaforget")
    async def gaforget(self, event: AstrMessageEvent) -> None:
        """清空本群的长期记忆（L3）。"""
        gid = event.get_group_id()
        if not gid:
            return
        n = self.ltm.total_facts(gid)
        self.ltm.forget(gid)
        await event.send(
            MessageChain().message(f"已清空群 {gid} 的长期记忆（{n} 条）。")
        )

    @filter.command("gareload")
    async def gareload(self, event: AstrMessageEvent) -> None:
        """热重载人格（从数据库重新读入内存）—— 不用重启 AstrBot。

        什么时候需要：直接用脚本 / 数据库改了 personas 表之后。AstrBot 的人格是
        **启动时缓存进内存**的（日志里的 `Loaded N personas`），改库不改内存＝不生效。
        WebUI 里改人格会自动刷新缓存，不需要这条命令。
        实现就是 AstrBot 自己的刷新套路（见 persona_mgr.batch_update_sort_order）：
        重读全部人格 + 重建 v3 缓存。
        """
        try:
            pm = self.context.persona_manager
            pm.personas = await pm.get_all_personas()
            pm.get_v3_persona_data()
            logger.info("[group_activation] 人格热重载完成")
            msg = "人格已从数据库热重载（不用重启 AstrBot）。"
        except Exception as e:  # noqa: BLE001
            logger.warning("[group_activation] 人格热重载失败: %s", e)
            msg = f"热重载失败：{e}"
        await event.send(MessageChain().message(msg))

    @filter.command("gareset")
    async def gareset(self, event: AstrMessageEvent) -> None:
        """重置本群的 L2 多轮历史（新建一个会话，旧历史不再进请求）。

        用途：模型把自己的坏习惯写进了历史（例如每条都 @ 同一个人），之后
        照着历史模仿（自我强化，见 fix_mentions 的说明）。L1/L3/L4 都不动。
        """
        umo = event.unified_msg_origin
        try:
            await self.context.conversation_manager.new_conversation(umo)
            msg = "已重置本群的多轮历史（L2）。L1/L3/知识库不受影响。"
            logger.info("[group_activation] 已重置 L2（%s）", umo)
        except Exception as e:  # noqa: BLE001
            logger.warning("[group_activation] 重置 L2 失败（%s）: %s", umo, e)
            msg = f"重置失败：{e}"
        await event.send(MessageChain().message(msg))

    async def _call_screen(self, event: AstrMessageEvent, action: str) -> bool:
        """调用桥的私有 OneBot 动作控制宿主机（Windows）屏幕。

        为什么绕这一圈：插件跑在 Linux 容器里，碰不到宿主机显示器；桥就在 Windows 上，
        由它去调 `scripts\\screen-off.ps1` 最直接（见 `bridge/main.py` 的
        `wake_screen` / `sleep_screen` 分支）。走 OneBot 连接，不需要额外端口或密钥。
        """
        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None)
        if call is None:
            logger.warning(
                "[group_activation] 当前平台不支持 call_action，屏幕控制不可用"
            )
            return False
        try:
            await call(action)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("[group_activation] 屏幕控制 %s 失败: %s", action, e)
            return False

    @filter.after_message_sent()
    async def _after_message_sent(self, event: AstrMessageEvent) -> None:
        """把「开屏」排在回复**发出之后**执行。

        为什么：切换显示拓扑会让微信窗口的分辨率/DPI 变化、布局缓存失效；若切换
        与发送重叠，输入框探测会连续失败并触发完整 OCR 重校准（实测一次发送 145 秒）。
        """
        if not event.get_extra("ga_wake_screen", False):
            return
        event.set_extra("ga_wake_screen", False)
        await self._call_screen(event, "wake_screen")

    @filter.command("gaon")
    async def gaon(self, event: AstrMessageEvent) -> None:
        """开屏：把宿主机从熄屏状态唤醒（黑屏挂机时远程点亮屏幕）。

        顺序刻意是「先回消息、再切屏幕」—— 反过来两者会互相打架。
        """
        await event.send(MessageChain().message("屏幕这就亮 🫧"))
        await self._call_screen(event, "wake_screen")

    @filter.command("gaoff")
    async def gaoff(self, event: AstrMessageEvent) -> None:
        """熄屏：让宿主机真黑屏（面板断电）而机器人继续跑；@ 我一下就开回来。"""
        await event.send(
            MessageChain().message("已熄屏 🌙 机器人在黑屏下照常跑；想开屏就 @ 我 或发 /gaon。")
        )
        await self._call_screen(event, "sleep_screen")

    @filter.command("gakb")
    async def gakb(self, event: AstrMessageEvent) -> None:
        """查看本群知识库（L4）概况。"""
        gid = event.get_group_id()
        if not gid:
            return
        try:
            msg = await self.kbs.stats(gid)
        except Exception as e:  # noqa: BLE001
            msg = f"查询失败：{e}"
        await event.send(MessageChain().message(msg))

    @filter.command("gakbforget")
    async def gakbforget(self, event: AstrMessageEvent) -> None:
        """清空本群知识库（L4）。"""
        gid = event.get_group_id()
        if not gid:
            return
        try:
            n = await self.kbs.forget(gid)
        except Exception as e:  # noqa: BLE001
            n = 0
            logger.warning("[group_activation] 清空知识库失败（群 %s）: %s", gid, e)
        await event.send(
            MessageChain().message(f"已清空群 {gid} 的知识库（{n} 条）。")
        )

    @filter.command("garemember")
    async def garemember(self, event: AstrMessageEvent, text: str = "") -> None:
        """手动往本群知识库写一条知识：/garemember 周三晚上通常没人。"""
        gid = event.get_group_id()
        if not gid:
            return
        content = (text or "").strip()
        if not content:
            await event.send(
                MessageChain().message("用法：/garemember 要记住的内容")
            )
            return
        try:
            _, detail = await self.kbs.remember(gid, [content])
        except Exception as e:  # noqa: BLE001
            detail = f"没写入：写入异常（{e}）"
            logger.warning("[group_activation] 手动写入知识库失败: %s", e)
        logger.info(
            "[group_activation] /garemember（群 %s）：%r → %s",
            gid,
            content[:40],
            detail,
        )
        await event.send(MessageChain().message(detail))
