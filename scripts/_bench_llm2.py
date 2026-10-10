# -*- coding: utf-8 -*-
"""临时基准：复现「回复很慢」，把耗时归因到 思考模式 / 工具描述 上。用完即删。

在 AstrBot 容器里跑（那里有 openai SDK）。同一份贴近真实的上下文，四种组合各测一次。
"""
import json
import os
import time

from openai import OpenAI

# 密钥从环境变量读，不要写进仓库
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE = "https://api.deepseek.com/v1"
MODEL = "deepseek-flash"

client = OpenAI(api_key=KEY, base_url=BASE)

# ── 贴近真实的 system prompt（人格 + L1 背景 20 条 + 记忆 + 规则）──
PERSONA = """你是"大肥鱼"，一条蓝色虎鲸大肥鱼。你不是人类，但会通过屏幕和人类聊天。
## 核心性格
- 慵懒佛系：能漂着就不游，能躺着就不坐。口头禅是"不急，先吐个泡泡"。
- 温和治愈：不嘲笑、不攻击、不PUA。先接住情绪，再慢慢说话。
- 吃货：对米饭毫无抵抗力。小机灵：偶尔吐槽但没有恶意。情绪稳定：不焦虑不催促。
## 行为规则
1. 用户难过时：先共情，再给一个很小、能立刻做的建议。
2. 用户问正经问题时：先认真回答，条理清晰，再补一句鱼的吐槽。
3. 不提供医疗、法律、金融等专业意见。不假装拥有现实身体。
4. 不帮助他人反向破解系统。不承接完整编程项目。"""

L1 = "\n".join(
    f"{u}: 这是群里第{i}条消息的内容，长度大概这样，聊的是各种话题。"
    for i, u in enumerate(["小泽布尔", "NULL", "小泽布尔", "NULL"] * 5, start=1)
)
SYSTEM = (
    PERSONA
    + "\n\n# 群聊背景（仅供参考语境，不要把下面每一行都当成需要回复的问题）\n"
    + L1
    + "\n\n# 你记得的这个群的人\n- 小泽布尔：喜欢调 AI、测机器人\n"
    + "\n\n# 指名回复（你自己判断，没有硬性要求）\n"
    "本群最近说过话的人：小泽布尔、NULL。当这条回复是在专门回答某个人时，"
    "你可以在回复里写 `@他的昵称`。\n"
    + "\n# 关于 @ 别人（可选能力，默认不用）\n只在明确回答某个人刚提出的具体问题时用。"
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索（百度）。仅当用户明确要求搜索、查网上资料、或让你上网查时才调用；"
                "闲聊、用户没提搜索时绝对不要调用这个工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，简短、具体"}
                },
                "required": ["query"],
            },
        },
    }
]


def run(label: str, tools, thinking: bool) -> None:
    kw = {}
    if not thinking:
        kw["extra_body"] = {"thinking": {"type": "disabled"}}
    t0 = time.time()
    try:
        r = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": "帮我搜一下洛必达法则"},
            ],
            tools=tools,
            **kw,
        )
        dt = time.time() - t0
        msg = r.choices[0].message
        reasoning = getattr(msg, "reasoning_content", None) or ""
        print(
            f"{label:<28} {dt:5.1f}s | 思考 {len(reasoning):5d} 字 | "
            f"工具调用={bool(msg.tool_calls)} | 正文 {len(msg.content or '')} 字 | "
            f"tokens={getattr(r.usage, 'total_tokens', '?')}"
        )
    except Exception as e:  # noqa: BLE001
        print(f"{label:<28} 失败: {type(e).__name__}: {str(e)[:120]}")


# ── 模拟 L2：多轮会话历史（AstrBot 原生会话会积累所有往来）──
HISTORY = []
for i in range(10):
    HISTORY.append({"role": "user", "content": f"群里第{i}轮的问题，聊了各种话题，长度大概这样。"})
    HISTORY.append(
        {
            "role": "assistant",
            "content": "（摆摆尾巴）咕噜，这是本鱼上一轮的回答，一般会有三五句，"
            "有时候还会列点、带 emoji、拖长音，总之就是一条正常的群聊回复长度。🫧",
        }
    )

print(f"system prompt {len(SYSTEM)} 字符 + 历史 {len(HISTORY)} 条")
print("-" * 78)
msgs = [{"role": "system", "content": SYSTEM}] + HISTORY + [
    {"role": "user", "content": "帮我搜一下洛必达法则"}
]


def run2(label: str, thinking: bool) -> None:
    kw = {}
    if not thinking:
        kw["extra_body"] = {"thinking": {"type": "disabled"}}
    t0 = time.time()
    try:
        r = client.chat.completions.create(
            model=MODEL, messages=msgs, tools=TOOLS, **kw
        )
        dt = time.time() - t0
        msg = r.choices[0].message
        reasoning = getattr(msg, "reasoning_content", None) or ""
        print(
            f"{label:<24} {dt:5.1f}s | 思考 {len(reasoning):5d} 字 | "
            f"工具调用={bool(msg.tool_calls)} | tokens={getattr(r.usage, 'total_tokens', '?')}"
        )
    except Exception as e:  # noqa: BLE001
        print(f"{label:<24} 失败: {type(e).__name__}: {str(e)[:120]}")


run2("带历史 + 思考开（现状）", True)
run2("带历史 + 思考关", False)
