"""Explicit send outcomes; uncertain delivery must never trigger a blind resend."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SendResult:
    state: str  # sent / failed / unknown
    detail: str = ""

    def __bool__(self):
        return self.state == "sent"

    @classmethod
    def from_gui(cls, response):
        if isinstance(response, dict):
            message = str(response.get("message") or "")
            if response.get("status") == "成功":
                return cls("sent", message)
            if "未确认" in message or "已操作发送" in message:
                return cls("unknown", message)
            return cls("failed", message or "微信发送失败")
        if response is True:
            return cls("sent")
        if response is False:
            return cls("failed", "微信发送失败")
        return cls("unknown", "微信发送接口未返回可确认的结果")
