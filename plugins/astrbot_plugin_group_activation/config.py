# -*- coding: utf-8 -*-
"""插件配置：默认值 + 从插件目录的 config.json 覆盖。

v0.3.0 起职责收窄：
  插件不再生成任何回复（全部交给 AstrBot 默认 LLM），
  @ 只是记忆窗口的开关，因此删除了 use_llm / reply_* / show_window_remaining。
"""

from __future__ import annotations

import io
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")


@dataclass
class PluginConfig:
    # ── 窗口（@ 开关）────────────────────────────────
    w0_seconds: float = 30.0            # 被 @ 后的初始窗口长度
    idle_seconds: float = 30.0          # 滑动空闲判定：最后一次活动后多久休眠
    window_max_seconds: float = 120.0   # 单次激活最长存活（"聊多久"的硬上限）

    # ── 并发 ─────────────────────────────────────────
    active_groups_max: int = 3          # 同时激活的群数上限

    # ── 记忆 ─────────────────────────────────────────
    record_l1: bool = True              # 是否常驻记录 L1 群聊滚动背景
    inject_l1: bool = True              # 是否把 L1 注入默认 LLM 请求
    l1_size: int = 10                   # L1 保留条数（2026-10-08 由 25 调小，压上下文 token）
    local_routing: bool = True          # 先本地筛掉无关消息，再决定是否调用模型
    history_max_turns: int = 8          # 每次请求携带的 L2 最近轮数
    history_max_chars: int = 12000      # L2 粗略字符预算（不是精确 token）
    # 带当前图的请求：识图通常只需要最近几轮；L2 是最大 token 项，优先压它。
    image_history_max_turns: int = 4
    image_history_max_chars: int = 6000
    # 纯文字窗口消息的 L1-only 接话判定；PASS 时不发完整请求。
    preflight_enable: bool = True
    preflight_timeout: float = 8.0
    l1_header: str = (
        "# 群聊背景（下面是本群最近的发言，每行开头「」里的就是说话的人；"
        "仅供参考语境，不要把每行都当成需要回复的问题；"
        "「[图片 #ID]」是缓存编号：需要看内容时**直接调用 look_image**，"
        "不要先回复「我看不到图」「我去调出来看看」之类的解释或预告；"
        "工具返回图片后再给最终回答）"
    )

    # ── 参与决策：让 LLM 有权「不发言」────────────────
    llm_decides_participation: bool = True
    pass_marker: str = "[PASS]"
    participation_prompt: str = (
        "# 参与规则（默认闭嘴，只在「接着刚才的话」时才开口）\n"
        "你在群里是配角，不是客服。**默认保持沉默**，只有两种情况才发言：\n"
        "1. 这条消息 @ 了你 → 必须回复。\n"
        "2. 这条消息（没 @ 你）**明确在延续你所在的对话** —— 直接回应/追问你刚\n"
        "   说的话，或点着名让你把刚才那件事说下去 —— 这时才接话。\n"
        "下面这些**一律不接**（只输出 {marker}，不要输出任何其它内容）：\n"
        "- 别人之间的对话，新开的、与你不相关的话题\n"
        "- 群里的一般闲聊、斗图、玩梗、刷屏\n"
        "- 讨论或评价你的行为、外貌、语气（除非直接问你）\n"
        "- 单字、语气词、纯表情、纯图片消息\n"
        "- 你拿不准是不是在跟你说话\n"
        "一句话判断标准：**这条消息和你刚说的那件事有关系吗？没关系就别接。**\n"
        "# 避免复读（决定接话时才适用）\n"
        "- 先看背景与历史里自己刚说过什么。如果这条消息问的正是你刚答过的事\n"
        "  （同一张图、同一个问题、同一件事）—— **不要复读同样的内容**：\n"
        "  · 有新信息/新角度 → 只说新的那部分；\n"
        "  · 没有新东西 → 一句话带过（如「上面说过了哈」），不要再展开一遍；\n"
        "- 不同的人问同一件事也一样，别给每个人都复制粘贴一遍。\n"
    )
    # 噪音消息不放行：窗口内的单字/纯表情/纯图片消息不值得触发 LLM 调用
    # （既白花 token，又容易换来一句多余的接话）。只记 L1、续窗口，不说话。
    skip_noise_messages: bool = True
    # 防复读闸门（确定性兜底）：窗口内消息（可沉默的）如果回复与自己**上一条**
    # 高度相似，直接不发。为什么需要：两人前后脚问同一张图时，模型会把同一段话
    # 再说一遍（群里看到的是「自说自话」，2026-10-08 实况）。被 @ 的必须回，
    # 不受这道闸门限制 —— 那条靠上面的提示词约束「别复读」。
    reply_dedup_enable: bool = True
    reply_dedup_score: float = 0.90

    # ── 指名回复：模型自己决定要不要 @ 某人 ────────────
    # 桥会在出站正文里找 `@本群成员名` 并换成微信**真实 @**（对方会收到提醒）。
    # 名单由这里注入，桥还会二次校验（只认本群真实成员）。
    enable_mention: bool = True
    mention_max_names: int = 12          # 注入名单时最多列几个名字
    # 硬校正：@ 只能指向**本条消息的发送者**，@ 别人会被改成发送者、多余 @ 去掉。
    # 为什么需要：模型会自我模仿 —— 历史回复里全是「@同一个人」，之后不管谁问
    # 都照抄（实测连「艾特我」都能 @ 错人）。提示词管不住，用确定性规则兜底。
    mention_only_sender: bool = True
    # 一条回复最多保留几个 @（仅 mention_only_sender=false 时生效）。
    # 1 = 只允许一个；>1 时模型可以在正文里 @ 本群多个成员，桥逐个点选。
    mention_max_count: int = 1
    # 被 @ 时是否强制「回复开头 @ 发送者」：
    #   False（默认）= 提示词只说“想 @ 就写”，模型不写也不自动补；
    #   True          = 旧行为：提示词要求必须 @，模型没写就自动补上。
    force_at_sender: bool = False
    mention_prompt: str = (
        "# 关于 @ 别人\n"
        "本群里能真正 @ 到的人：{names}\n"
        "在回复里写 `@某个人的昵称`，微信就会真的 @ 到他。\n"
        "**你可以 @ 本群任何成员，不要求对方先在本批次发言。**\n"
        "想让谁收到提醒、想把哪段内容转给谁，就在那段开头写 `@他的名字`。\n"
        "发送者说「告诉X / 提醒X / 转告X」时，**必须**在给 X 的那段开头写 `@X`，"
        "否则 X 收不到提醒。\n"
        "一条消息最多 @ {max_mentions} 个人；不要重复 @ 同一个人。\n"
        "被 @ 时想 @ 回对方就写，不写也可以。\n"
        "如果使用 send_message_to_user 发正文，它的 plain 文本里也必须写 `@昵称`；"
        "不要只把 @ 放进 mention_user 的 ID 里（桥认的是正文里的 @名字）。\n"
        "不要把正文拆成「标题 + 另一条消息」；一次把完整正文写出来。\n"
        "不要在查资料前先发「我先去查一下」之类的预告；查完直接给完整正文。\n"
        "名字必须用上面列出的（写别的不会生效，只会变成普通文字）。\n"
    )

    # ── 后台 Playwright 浏览器搜索（无需搜索 API Key）──
    # 每日次数是本地资源上限，与搜索供应商免费额度无关。
    search_enable: bool = True
    search_daily_limit: int = 50   # 跨群共享的本地请求上限，发出前预留
    search_top_k: int = 6          # 每次搜索返回的条目数

    amap_enable: bool = True
    amap_daily_limit: int = 100   # 每次地点搜索和每段路线各计一次
    website_enable: bool = True
    website_daily_limit: int = 300
    website_moderation_timeout: float = 15.0

    # ── L3 长期记忆（结构化「群员画像」）──────────────
    l3_enable: bool = True
    l3_min_new_messages: int = 5        # 窗口内至少积累这么多条消息才值得提炼
    l3_cooldown_seconds: float = 300.0  # 同一群两次提炼的最小间隔（防频繁调 LLM）
    l3_max_members: int = 8             # 注入时最多带几个成员
    l3_max_facts_per_member: int = 4    # 注入时每人最多几条
    l3_facts_cap_per_member: int = 12   # 存储时每人最多保留几条（超出丢最久未提及的）
    # 写入前的**语义去重**：与本人已有事实相似度 ≥ 阈值就跳过。
    # 为什么需要：add_facts 只按文本精确匹配去重，换个说法就重复入库
    # （实测「会搓工具传GitHub」与「会写代码愿分享到GitHub」并存）。
    l3_dedup_enable: bool = True
    l3_dedup_score: float = 0.90        # 与 L4 查重同一套自算余弦
    l3_header: str = "# 你记得的这个群的人（长期记忆，仅供参考，不要逐条复述）"
    l3_extract_prompt: str = (
        "# 任务\n"
        "从下面的群聊记录中提炼**关于群成员的、长期有效的事实**，供以后记住这些人。\n\n"
        "# 要记录\n"
        "- 性格、说话习惯、常用称呼\n"
        "- 稳定的爱好、职业/专业、擅长的事\n"
        "- 明确的偏好或忌讳\n\n"
        "# 不要记录\n"
        "- 一次性的内容（今天吃了什么、临时安排、当次的具体问题）\n"
        "- 关于你自己（机器人）的人设设定 —— 那不是成员信息\n"
        "- 推测、脑补、玩笑话\n\n"
        "# 输出\n"
        "严格输出 JSON，不要任何解释、不要代码块标记：\n"
        '{"facts": [{"member": "昵称", "fact": "一句话事实，不超过20字"}]}\n'
        '若没有值得记录的，输出 {"facts": []}\n\n'
        "# 群聊记录\n"
    )

    # ── L4 群公共知识库（AstrBot 知识库：自动学 + 语义检索注入）──
    kb_enable: bool = True
    kb_name_prefix: str = "群聊记忆-"       # 每群一个库：<prefix><群id>
    kb_embedding_provider_id: str = "ollama/bge-m3"  # provider 实例 ID（指向来源 ollama_embedding）
    kb_query_max_chars: int = 1000
    kb_retrieval_timeout: float = 5.0  # 整次检索总超时，不按子步骤累计
    kb_daily_limit: int = 20               # 每天最多自动写入多少条（防爆库）
    kb_dedup_score: float = 0.90           # 检索相似度 ≥ 此值视为已存在，跳过写入
    kb_inject_top: int = 3                 # 注入时最多带几条
    kb_min_score: float = 0.35             # 注入的最低相关度（滤掉不相关命中）
    kb_max_chars: int = 120                # 单条知识最长字符数
    kb_header: str = (
        "# 本群沉淀的公共知识（长期记忆，按当前话题检索所得，仅供参考）\n"
        "# 每条括号里的日期是记录时间：越久远越可能过时，仅作参考，不要当成当下事实复述"
    )
    # 追加在 L3 提炼 prompt 的「# 群聊记录」小节之前 —— 同一次 LLM 调用，
    # 顺带产出群公共知识，不多花一次调用。改它无需动 main.py。
    kb_extract_appendix: str = (
        "\n# 额外任务\n"
        "除了成员事实，再从同一份记录里提炼**关于这个群的公共知识**：\n"
        "大家形成的梗、约定、共同经历、反复讨论得出的结论 —— 只要长期有用，\n"
        "且不属于任何个人画像（关于某个人的归到 facts，不要重复）。\n"
        "在 JSON 里多输出一个键：\n"
        '{"knowledge": ["一句话知识，不超过40字"]}\n'
        "没有就输出空数组。\n\n"
    )

    # ── 多人提问合并 ─────────────────────────────────
    # 首条放行事件等待，其余明确 @ 和参与者补充只进入本地批次。
    # 静默5秒派发；连续补充也不能超过首条起30秒。图片不重置计时。
    merge_enable: bool = True
    merge_seconds: float = 5.0
    task_buffer_seconds: float = 120.0
    task_buffer_max_entries: int = 200
    task_buffer_max_chars: int = 12000
    merge_max_seconds: float = 30.0

    # ── 历史里的图片不再重发 ─────────────────────────
    # 为什么：L2 会话历史里的图片是以 base64 存在消息里的，**之后每一轮请求都会
    # 重新发一遍**。实测（2026-10-10，deepseek-flash）：一张 1280px 的图 ≈ 947
    # input token，群里几十张图就是每轮几万 token（当时单次请求 6.6 万 token 的
    # 主要来源之一），而输出只有几百 token —— 等于钱全花在重发旧图上。
    # 剥掉之后：文字消息（含它当时对着图的回答）照旧在历史里，记忆不丢；
    # 当轮新发的图不受影响（走 req.image_urls，仍会真正送给多模态模型）；
    # 「先发图、再追问」那种紧跟的追问由 image_carry_seconds 兜底。
    strip_history_images: bool = True
    # 剥掉图之后给这条消息补的占位文字（保持「当时有人发过图」的信息）
    stripped_image_placeholder: str = "[图片]"

    # ── 无内容消息（图片 / 表情包 / 单个标点 / 单字）：默认不回 ────────
    # 这类消息本身没有语义，判不了它跟话题有没有关系 —— 所以**从群友名字入手**：
    # 这个人的名字最近是否正在跟它对话（他刚 @ 过它 / 叫过它的昵称 / 它刚在回他）。
    # 对得上 → 放行（典型：@ 它之后紧接着甩一张图，那是发给它看的）；
    # 对不上 → 只记 L1、不回（2026-10-09 用户要求）。
    skip_contentless_messages: bool = True
    name_link_seconds: float = 45.0     # 「名字」往前追溯的时长
    # 「先发图、再问一句」：后一条纯文字消息拿不到图（图片只在它自己那条事件里送给模型），
    # 所以把图缓存下来补挂到后一条的请求上；这个时长内有效（只补挂一次）。
    image_carry_seconds: float = 30.0
    # 缓存图片按编号查看：每轮最多取几张 / 最多列几次目录（防工具循环）
    image_look_max_per_turn: int = 10
    image_list_max_per_turn: int = 2
    image_list_limit: int = 20
    bot_names: list[str] = field(default_factory=list)
    # ↑ 机器人自己的昵称补充（自动从桥的 OneBot get_login_info 取，这里只补别名/错字）

    # ── 空 @（只 @ 了它、没有正文）：把「他刚说的那句」当成本次要回的内容 ──
    # 微信用法常见两种顺序（先打字再 @ / 先 @ 再打字），以及「问了没被理，再 @ 一次」。
    # 实测（2026-10-09）：单发一个「@它」时框架没把它标成 @，插件当普通消息沉默处理 →
    # 连窗口都没开、L1 也没注入 → 群里表现就是「@ 了它，它却看不到你上一条」。
    bare_at_lookback_seconds: float = 180.0   # 往前找多久内同一人说的那句（0 = 关闭）

    # ── 屏幕控制（经桥调用宿主机 scripts\screen-off.ps1）──────────
    # 被 @ 时是否顺手唤醒宿主机屏幕。**默认关闭**：
    #   * 每次 @ 都会切一次显示拓扑，而切换与消息发送撞上会触发一次 OCR 重校准，
    #     实测把一条发送拖到 145 秒（2026-10-09 10:36）；
    #   * 群里别人 @ 它时也会亮屏，不符合"安静挂机"的预期。
    # 要手动控制就用命令：/gaon 开屏、/gaoff 熄屏。
    wake_screen_on_at: bool = False

    # ── 其它 ─────────────────────────────────────────
    ignore_prefixes: list = field(default_factory=lambda: ["/"])

    @classmethod
    def load(cls) -> "PluginConfig":
        cfg = cls()
        if CONFIG_PATH.exists():
            try:
                with io.open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                with io.open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(asdict(cfg), f, ensure_ascii=False, indent=2)
            except Exception:  # noqa: BLE001
                pass
        return cfg
