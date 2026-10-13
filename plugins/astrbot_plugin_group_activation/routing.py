# -*- coding: utf-8 -*-
"""本地群聊路由：在付费模型调用之前做保守的相关性筛选。"""

from __future__ import annotations

import copy
import re


_FOLLOWUP = re.compile(
    r"[?？]|^(?:那|所以|但是|不过|可是|对了|等等|不对|继续|再说|还有|刚才|"
    r"为什么|怎么|如何|哪个|什么|是不是|能不能|可以|请|帮我|你|这|那个)"
    r"|(?:展开|解释|详细|举例|接着|看图|图片|截图|照片|这张图|这是什么|太难了)"
)
_IMAGE_QUERY = re.compile(
    r"图|照片|截图|表情包|画面|看一下|看看|认一认|识别|读字|文字|上面写|这是什么"
)
_NEW_TOPIC = re.compile(r"^(?:大家|有人|你们|群里|谁)(?:知道|觉得|会|能|有)?")
_ACK = {"嗯", "嗯嗯", "哦", "好", "好的", "收到", "谢谢", "哈哈", "哈哈哈", "ok", "nice"}
_REQUEST = re.compile(
    r"[?？]|(?:为什么|怎么|如何|多少|几个|什么|哪[个里]|能不能|可以吗|是不是)"
    r"|^(?:(?:来吧|请|帮我|麻烦你|你|再|继续|先)[，,、\s]*)*"
    r"(?:数数|数一下|计数|计算|算一下|写|翻译|解释|总结|分析|检查|修改|列出|举例|展开)"
)
_COUNT_TASK = re.compile(r"数数|数一下|计数|数到\s*\d+")
_NUMBERS = re.compile(r"[+-]?\d+(?:\.\d+)?(?:[\s,，、;；]+[+-]?\d+(?:\.\d+)?)*")


def is_explicit_request(text: str) -> bool:
    t = (text or "").strip()
    return bool(t and t.lower() not in _ACK and not _NEW_TOPIC.match(t) and _REQUEST.search(t))


def starts_count_task(text: str) -> bool:
    return bool(_COUNT_TASK.search((text or "").strip()))


def is_number_content(text: str) -> bool:
    return bool(_NUMBERS.fullmatch((text or "").strip()))


def is_count_question(text: str) -> bool:
    return bool(re.search(r"(?:多少|几个).*数|数.*(?:多少|几个)", text or ""))


def search_intent(text: str) -> bool | None:
    """Only the current speaker's explicit request grants search; quotes do not."""
    t = re.sub(r"「[^」]*」|“[^”]*”|\"[^\"]*\"", "", text or "")
    t = re.sub(r"\[At:\d+\]", "", t).strip()
    if re.search(r"(?:不要|不用|无需|不必|不需要|别|禁止|取消|停止|先不)(?:再|去|帮我|进行|联网|百度)*\s*(?:搜索|搜|上网|联网|查|search)", t, re.I):
        return False
    if re.search(r"(?:搜索|联网|search)(?:是什么意思|是什么|怎么用|功能)|(?:能|会|可以)(?:联网)?(?:搜索|search)(?:吗|么)[?？。]*$", t, re.I):
        return None
    if re.search(r"(?:上网|联网|网上|百度)(?:.{0,5})(?:搜|查|找)|(?:帮我|请|麻烦你|给我|再|继续)\s*(?:搜|search)|^(?:我想|我要|你)?\s*(?:搜索|搜一下|搜搜|search\b|look\s+up\b)|百度一下", t, re.I):
        return True
    return None


def kb_query(texts: list[str], max_chars: int = 1000) -> str:
    """Use question text consistently, without batch JSON or sender metadata."""
    questions = []
    for text in texts:
        text = re.sub(r"\[At:\d+\]|\[(?:图片|表情包|语音|空 @)\]", "", text or "").strip()
        if text and not is_number_content(text) and text not in questions:
            questions.append(text)
    return "\n".join(questions)[-max(1, int(max_chars)):]


def wants_image(text: str) -> bool:
    """只有文字确实在问图，才把上条缓存的图带进多模态请求。"""
    return bool(_IMAGE_QUERY.search((text or "").strip()))


def should_consider_followup(text: str, *, linked: bool) -> bool:
    """未 @ 的消息只有近期对话参与者的明显续话才可能花钱生成。"""
    t = (text or "").strip()
    if not linked or not t or t.lower() in _ACK or _NEW_TOPIC.match(t):
        return False
    return search_intent(t) is not None or is_explicit_request(t) or bool(_FOLLOWUP.search(t))


def compact_contexts(contexts: list[dict], *, max_turns: int, max_chars: int) -> list[dict]:
    """请求时只带最近若干完整轮次，历史图以占位替换，不重复计图像 token。

    从用户消息划分轮次，避免把 tool 结果同其发起的 assistant 调用拆散。
    不修改传入的上下文对象；provider 随后可能把新上下文写回会话。
    """
    if not contexts:
        return []
    turns: list[list[dict]] = []
    for message in contexts:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user" or not turns:
            turns.append([])
        turns[-1].append(copy.deepcopy(message))
    turns = turns[-max(1, int(max_turns)):]
    for turn in turns:
        for message in turn:
            content = message.get("content")
            if isinstance(content, list):
                new_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        new_parts.append({"type": "text", "text": "[旧图片略；如需看图请重新发送]"})
                    else:
                        new_parts.append(part)
                message["content"] = new_parts
    # 以完整轮次为单位压缩，至少保留最近一轮。
    budget = max(1000, int(max_chars))
    while len(turns) > 1 and sum(len(str(m)) for t in turns for m in t) > budget:
        turns.pop(0)
    # 一条历史消息本身超预算时保留尾部，避免单次粘贴长文让后续每轮都超额。
    for turn in turns:
        for message in turn:
            content = message.get("content")
            if isinstance(content, str) and len(content) > budget // 2:
                message["content"] = "[旧消息开头略]" + content[-budget // 2:]
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        value = part.get("text")
                        if isinstance(value, str) and len(value) > budget // 2:
                            part["text"] = "[旧消息开头略]" + value[-budget // 2:]
    return [message for turn in turns for message in turn]
