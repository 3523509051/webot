# -*- coding: utf-8 -*-
"""私聊授权门：把"名单同步结果"翻译成读取目标 + 投递闸门 + 读取起点。

职责只有三件（不碰微信数据库细节，`latest_seq` 由调用方注入）：

1. **按稳定 wxid 增删目标**：成功空名单 = 真实撤销（停读 + 丢待投递事件并计数）；
   `ok=False` = 暂时不可验证（暂停投递，**队列与水位原样保留**）。
2. **读取起点**：开通码事件 → 撤销后重新授权 → 启动快照 → "现在"，四级明确，
   任何一级都不回放旧聊天。
3. **记住撤销水位**：重新授权从"现在"继续，撤销期间的聊天不补发。

用 `UnifiedPoller` 的投递闸门暂停，因此暂停期间读取与入队照常（队列满自然背压），
恢复后先处理已保留的那条，顺序与幂等都由调用方保证。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Dict, Iterable, Optional

from unified_poll import KIND_PRIVATE, UnifiedPoller

log = logging.getLogger("bridge.private")


class WatermarkUnknown(RuntimeError):
    """无法确定**可信**读取起点。

    绝不把"查不到"当成 0：0 意味着"从头读"，会把整段历史当成新消息回放。
    遇到这个异常，调用方必须**暂缓新增目标**并在下一次同步重试，不能退化成 0。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def collect_startup_seq(
    sessions: Iterable, latest_seq: Callable[[str], int]
) -> Dict[str, int]:
    """启动阶段逐会话取快照水位。

    `latest_seq` 必须是**严格**入口（失败抛异常）。单个会话查不到就**不记录**
    （绝不写 0：0 表示从头读，会把历史当新消息），也不影响其它会话。
    """

    result: Dict[str, int] = {}
    skipped = 0
    for row in sessions:
        peer = str((row or {}).get("username") or "") if isinstance(row, dict) else ""
        if not peer:
            continue
        try:
            seq = latest_seq(peer)
        except Exception as exc:  # noqa: BLE001 - 单会话失败不影响整体
            skipped += 1
            log.warning(
                "[私聊] 启动水位读取失败（%s）：该会话不记快照，后续按实时水位重试",
                type(exc).__name__,
            )
            continue
        result[peer] = max(0, int(seq))
    if skipped:
        log.warning(
            "[私聊] 启动快照跳过 %d 个会话（水位未知，绝不按 0 处理）", skipped
        )
    return result


class PrivateGate:
    """私聊授权状态机：只管理"哪些会话可读、从哪读、还能不能投递"。"""

    def __init__(
        self,
        poller: UnifiedPoller,
        latest_seq: Callable[[str], Awaitable[int]],
        startup_seq: Optional[Dict[str, int]] = None,
        account_id: str = "",
    ) -> None:
        self._poller = poller
        self._latest_seq = latest_seq
        self._startup = {str(k): int(v) for k, v in (startup_seq or {}).items()}
        self._account_id = str(account_id)
        #: 待用的开通码水位：兑换成功但名单还没同步时先存这里。
        self.pending_starts: Dict[str, int] = {}
        #: 已撤销会话的最终读取水位。
        self.retired: Dict[str, int] = {}
        #: 当前生效名单（稳定 `user_ref` → grant），**原地更新**，调用方可长期持有引用。
        self.grants: Dict[str, dict] = {}
        #: 因"起点未知"被暂缓的会话（wxid → 失败原因），下次同步自动重试。
        self.deferred: Dict[str, str] = {}
        self.paused = False
        self.added = 0
        self.revoked = 0
        # 丢弃计数以调度层为准（排队事件 + 撤销时正被持有的事件都算），
        # 这里只记住基线，避免两套计数各说各话。
        self._dropped_base = int(self._poller.metrics().get("dropped", 0))

    # ------------------------------------------------------------------ #
    # 查询 / 状态
    # ------------------------------------------------------------------ #
    @property
    def dropped(self) -> int:
        """撤销/等待期间被丢弃的待投递事件总数（含正被持有的那条）。"""

        return max(
            0, int(self._poller.metrics().get("dropped", 0)) - self._dropped_base
        )

    def contains(self, wxid: str) -> bool:
        return self._poller.has_target(KIND_PRIVATE, wxid)

    def target_count(self) -> int:
        return len(self._poller.targets(KIND_PRIVATE))

    # ------------------------------------------------------------------ #
    # 起点
    # ------------------------------------------------------------------ #
    async def _now(self, wxid: str) -> int:
        """取"从此刻开始"的真实水位；取不到就抛 `WatermarkUnknown`，**不回落到 0**。

        真实返回 0 的空会话是合法起点（首次消息就是 1），只有"查询失败/值非法"
        才算未知——两者必须区分，否则会把整段历史当成新消息回放。
        """

        try:
            value = await self._latest_seq(wxid)
        except Exception as exc:  # noqa: BLE001
            raise WatermarkUnknown(type(exc).__name__) from exc
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise WatermarkUnknown("invalid_value") from None
        if value < 0:
            raise WatermarkUnknown("negative_value")
        return value

    async def _start_for(self, wxid: str) -> tuple[int, str]:
        """返回 `(起点, 来源)`；调用方**成功建目标后**才消费 pending/retired。"""

        if wxid in self.pending_starts:
            # 开通码事件已确认处理：从它的 seq 之后继续，中间消息一条不跳。
            return self.pending_starts[wxid], "pending"
        if wxid in self.retired:
            # 撤销后重新授权：从"现在"继续，撤销期间的聊天不补发。
            return await self._now(wxid), "retired"
        if wxid in self._startup:
            # 进程启动就存在的授权会话：用启动快照，启动后到达的消息全读。
            return self._startup[wxid], "startup"
        return await self._now(wxid), "now"

    def remember_redeem(self, wxid: str, seq: int) -> None:
        """记下开通码事件的水位，作为该会话的常规读取起点。"""

        self.pending_starts[str(wxid)] = max(0, int(seq))

    # ------------------------------------------------------------------ #
    # 暂停 / 恢复
    # ------------------------------------------------------------------ #
    def set_paused(self, paused: bool, reason: str = "") -> None:
        if self.paused == paused:
            return
        self.paused = paused
        self._poller.set_paused(KIND_PRIVATE, paused)
        if paused:
            log.warning(
                "[私聊] 授权名单暂时不可验证（%s）：暂停私聊投递，保留队列与水位",
                reason or "unknown",
            )
        else:
            log.info("[私聊] 授权名单已恢复：继续投递已保留的私聊事件")

    # ------------------------------------------------------------------ #
    # 应用名单
    # ------------------------------------------------------------------ #
    async def apply(
        self, grants: Dict[str, dict], starts: Optional[Dict[str, int]] = None
    ) -> None:
        """应用一次**成功**同步得到的名单（空字典 = 全部撤销）。"""

        if starts:
            for wxid, seq in starts.items():
                self.remember_redeem(str(wxid), int(seq))

        self.grants.clear()
        self.grants.update(grants)

        wanted: Dict[str, dict] = {}
        for ref, item in grants.items():
            if not isinstance(item, dict):
                continue
            wxid = str(item.get("user_wxid") or "")
            if wxid and str(item.get("user_ref") or "") == str(ref):
                wanted[wxid] = item

        # 已不在名单里的暂缓项不再保留（避免状态越积越多）。
        for wxid in [k for k in self.deferred if k not in wanted]:
            self.deferred.pop(wxid, None)

        dropped_before = self.dropped
        revoked = 0
        for target in list(self._poller.targets(KIND_PRIVATE)):
            if target.ref in wanted:
                continue
            # 真实撤销：停读、丢弃排队事件并计数；记住最终水位防止回放。
            self._poller.remove_target(KIND_PRIVATE, target.ref)
            self.retired[target.ref] = target.last_seq
            self.pending_starts.pop(target.ref, None)
            revoked += 1
        dropped = max(0, self.dropped - dropped_before)

        added = 0
        deferred = 0
        for wxid, item in wanted.items():
            display = str(item.get("display_name") or wxid)
            if self._poller.has_target(KIND_PRIVATE, wxid):
                current = self._poller.get_target(KIND_PRIVATE, wxid)
                if current is not None:
                    current.name = display
                self.deferred.pop(wxid, None)
                continue
            try:
                start, source = await self._start_for(wxid)
            except WatermarkUnknown as exc:
                # 起点未知：**暂缓新增目标**，保留 pending/retired 供下次重试。
                # 不查询、不建目标，因此这段时间不会从 0 读历史。
                self.deferred[wxid] = exc.reason
                deferred += 1
                continue
            # 起点确定后才消费 pending/retired，失败时不会被提前吃掉。
            if source == "pending":
                self.pending_starts.pop(wxid, None)
            elif source == "retired":
                self.retired.pop(wxid, None)
            self.deferred.pop(wxid, None)
            self._poller.add_target(KIND_PRIVATE, wxid, display, max(0, int(start)))
            added += 1

        self.added += added
        self.revoked += revoked
        if added or revoked or dropped or deferred:
            # 只记数量与受控原因，不打印 wxid/正文。
            log.info(
                "[私聊] 授权名单变化：新增 %d 个会话，撤销 %d 个会话"
                "（丢弃待投递 %d 条），起点未知暂缓 %d 个",
                added, revoked, dropped, deferred,
            )
        if deferred:
            log.warning(
                "[私聊] 有 %d 个候选会话的读取起点无法确定（%s），本次不新增，"
                "下一次同步重试；不会从 0 开始读历史",
                deferred,
                ",".join(sorted(set(self.deferred.values())))[:80],
            )


__all__ = ["PrivateGate", "WatermarkUnknown", "collect_startup_seq"]
