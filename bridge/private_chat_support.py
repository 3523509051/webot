# -*- coding: utf-8 -*-
"""
私聊支持模块：授权同步、消息轮询、开通码兑换。

此模块提供桥层私聊功能的完整框架。
实际微信数据库操作需要根据真实环境调整。
"""
from __future__ import annotations

import asyncio
import logging
import time
import zlib
from typing import Dict, Optional, Set

log = logging.getLogger("bridge.private")

# 授权缓存：{user_ref: {wxid, conversation_id, agent_id, status_revision}}
_AUTHORIZED_CACHE: Dict[str, dict] = {}
_CACHE_LAST_UPDATE = 0.0
_CACHE_REFRESH_INTERVAL = 30.0  # 30秒刷新一次

# 未授权提示限频：{user_ref: last_hint_time}
_HINT_COOLDOWN = 3600  # 1小时
_LAST_HINT: Dict[str, float] = {}


def _fake_id(wxid: str) -> int:
    """将微信ID转换为OneBot假ID（与main.py保持一致）。"""
    return zlib.crc32(wxid.encode()) % (10**9)


async def sync_authorized_privates(pero_api_base: str, internal_token: str) -> dict:
    """从Pero Core同步已授权私聊列表。
    
    Args:
        pero_api_base: Pero API基础URL，如 "http://localhost:6198"
        internal_token: 内部服务Bearer token
        
    Returns:
        {user_ref: {wxid, conversation_id, agent_id, status_revision}}
    """
    global _AUTHORIZED_CACHE, _CACHE_LAST_UPDATE

    now = time.time()
    if now - _CACHE_LAST_UPDATE < _CACHE_REFRESH_INTERVAL:
        return _AUTHORIZED_CACHE

    try:
        import aiohttp

        url = f"{pero_api_base}/internal/v1/private/authorized"
        headers = {"Authorization": f"Bearer {internal_token}"}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=5) as resp:
                if resp.status != 200:
                    log.warning(f"授权同步失败: HTTP {resp.status}")
                    return _AUTHORIZED_CACHE

                data = await resp.json()
                privates = data.get("privates", [])

                # 更新缓存
                new_cache = {}
                for item in privates:
                    user_ref = item["user_ref"]
                    new_cache[user_ref] = {
                        "wxid": item.get("user_wxid"),
                        "conversation_id": item["conversation_id"],
                        "agent_id": item["agent_id"],
                        "status_revision": item["status_revision"],
                    }

                _AUTHORIZED_CACHE = new_cache
                _CACHE_LAST_UPDATE = now
                log.info(f"授权同步成功: {len(new_cache)}个私聊")

    except Exception as e:
        log.error(f"授权同步异常: {e}")

    return _AUTHORIZED_CACHE


def is_authorized(user_ref: str) -> bool:
    """检查用户是否已授权（从缓存）。"""
    return user_ref in _AUTHORIZED_CACHE


def get_authorized_wxids() -> Set[str]:
    """获取所有已授权的wxid集合。"""
    return {info["wxid"] for info in _AUTHORIZED_CACHE.values() if info.get("wxid")}


async def handle_unauthorized_private(
    user_ref: str,
    wxid: Optional[str],
    text: str,
    message_id: str,
    self_id: int,
    pero_api_base: str,
    internal_token: str,
    send_callback,
) -> bool:
    """处理未授权私聊消息（开通码兑换）。
    
    Args:
        user_ref: OneBot user_id (fake_id)
        wxid: 微信真实ID
        text: 消息文本
        message_id: 消息ID
        self_id: 机器人账号
        pero_api_base: Pero API基础URL
        internal_token: Bearer token
        send_callback: 发送回执的回调函数 async def(user_id, message)
        
    Returns:
        True if redeemed successfully, False otherwise
    """
    code = text.strip()

    # 快速模式匹配
    if not code.startswith("PERO-") or len(code) > 50:
        # 无效格式：限频提示
        if _should_send_hint(user_ref):
            try:
                await send_callback(
                    int(user_ref), "您暂未开通对话权限。如有开通码，请直接发送。"
                )
            except Exception as e:
                log.warning(f"发送未授权提示失败: {e}")
        return False

    # 调用Core兑换
    try:
        import aiohttp

        url = f"{pero_api_base}/internal/v1/private/redeem"
        headers = {
            "Authorization": f"Bearer {internal_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "channel_id": "onebot11",
            "account_id": str(self_id),
            "user_ref": str(user_ref),
            "user_wxid": wxid,
            "activation_code": code,
            "source_event_id": message_id,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, json=payload, timeout=10
            ) as resp:
                if resp.status != 200:
                    log.warning(f"兑换请求失败: HTTP {resp.status}")
                    await send_callback(int(user_ref), "✗ 兑换失败，请稍后重试")
                    return False

                result = await resp.json()

                if result.get("success"):
                    role_info = result.get("role_info", "已开通私聊对话")
                    await send_callback(int(user_ref), f"✓ {role_info}\n欢迎使用！")

                    # 刷新授权缓存
                    global _CACHE_LAST_UPDATE
                    _CACHE_LAST_UPDATE = 0  # 强制下次刷新
                    await sync_authorized_privates(pero_api_base, internal_token)

                    log.info(
                        f"开通码兑换成功: user_ref={user_ref}, is_owner={result.get('is_owner')}"
                    )
                    return True
                else:
                    error = result.get("error", "兑换失败")
                    await send_callback(int(user_ref), f"✗ {error}")
                    log.warning(f"开通码兑换失败: {error}")
                    return False

    except Exception as e:
        log.error(f"兑换过程异常: {e}")
        try:
            await send_callback(int(user_ref), "✗ 系统错误，请稍后重试")
        except:
            pass
        return False


def _should_send_hint(user_ref: str) -> bool:
    """判断是否应该发送未授权提示（限频）。"""
    now = time.time()
    last = _LAST_HINT.get(user_ref, 0)

    if now - last > _HINT_COOLDOWN:
        _LAST_HINT[user_ref] = now
        return True
    return False


def extract_text_from_message(message_segments: list) -> str:
    """从OneBot消息段中提取纯文本。"""
    texts = []
    for seg in message_segments:
        if isinstance(seg, dict) and seg.get("type") == "text":
            data = seg.get("data", {})
            if isinstance(data, dict):
                text = data.get("text", "")
                if text:
                    texts.append(str(text))
    return "".join(texts).strip()


# ==================== 占位：微信数据库私聊消息读取 ====================
# TODO: 需要探测实际微信数据库私聊表结构后实现


async def poll_private_messages(wechat_db, authorized_wxids: Set[str], last_seq: dict):
    """从微信数据库轮询私聊消息（占位实现）。
    
    Args:
        wechat_db: 微信数据库连接（WeChatDB实例）
        authorized_wxids: 已授权的wxid集合
        last_seq: {wxid: last_local_id} 上次读取位置
        
    Yields:
        原始消息字典
        
    TODO: 实际实现需要：
    1. 探测私聊消息表结构（可能是MSG表，type=1）
    2. 按wxid过滤（talker字段？）
    3. 增量读取（基于localId或createTime）
    4. 解析消息内容、附件等
    
    参考：见 PRIVATE_CHAT_TODO.md
    """
    # 占位实现：返回空
    return
    yield  # 使其成为生成器


def build_private_message_event(
    msg: dict, self_id: int, wechat_client
) -> Optional[dict]:
    """构建私聊消息OneBot事件（占位实现）。
    
    Args:
        msg: 微信数据库原始消息行
        self_id: 机器人账号ID
        wechat_client: WeChatClient实例
        
    Returns:
        OneBot v11事件字典，或None（如果无法构建）
        
    TODO: 见 PRIVATE_CHAT_TODO.md
    """
    # 占位实现
    return None


# ==================== 占位：私聊消息发送 ====================
# TODO: 需要确认wechatauto库如何打开私聊窗口


async def send_private_message(wechat_gui, wxid: str, text: str) -> dict:
    """发送私聊消息（占位实现）。
    
    Args:
        wechat_gui: WeChatGUI实例
        wxid: 对方微信ID
        text: 消息文本
        
    Returns:
        {'success': bool, 'message_id': str}
        
    TODO: 见 PRIVATE_CHAT_TODO.md
    """
    log.warning(f"send_private_message 未实现: wxid={wxid}, text={text[:50]}")
    return {"success": False, "message_id": ""}


__all__ = [
    "sync_authorized_privates",
    "is_authorized",
    "get_authorized_wxids",
    "handle_unauthorized_private",
    "poll_private_messages",
    "build_private_message_event",
    "send_private_message",
    "extract_text_from_message",
]
