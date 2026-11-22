# -*- coding: utf-8 -*-
"""群聊 / 已授权私聊的统一增量读取调度。

两层结构，互相解耦：

- **读取层（一个任务）**：按会话公平轮转会读取小批增量事件，只做数据库查询与
  水位推进。任何会话都不会因为"排在前面"而长期优先，慢会话也不会占住读循环。
- **分发层（每会话一个任务）**：图片解密、文件入库、构建消息段、OneBot 上报都在
  各自的串行队列里做。一个群在处理图片时，其它群/私聊照常读取与上报。

背压策略：队列满时**停止读取该会话并保留水位**，下一轮从同一位置继续 ——
既不丢事件，也不重复上报（水位只随已入队事件推进）。

本模块只做调度，不碰微信数据库细节：真实查询由 `read(kind, ref, since_seq)` 注入。
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

log = logging.getLogger("bridge.poll")

KIND_GROUP = "group"
KIND_PRIVATE = "private"


def next_backoff(
    delay: float, *, factor: float = 1.6, ceiling: float = 10.0
) -> float:
    """下一次重试间隔：**用已截顶的 delay 递推**，永不计算无界指数。

    旧写法 `base * 1.6 ** (attempt-1)` 在长时间断线（约 1700 次以后）会抛
    `OverflowError`，直接把该会话的分发任务打挂。这里始终返回
    `[0.05, ceiling]` 内的有限值。
    """

    try:
        value = float(delay) * float(factor)
    except (OverflowError, ValueError):
        return float(ceiling)
    if not math.isfinite(value):
        return float(ceiling)
    return min(float(ceiling), max(0.05, value))


def event_seq(event: Any) -> int:
    """取事件水位：只认真实整数，缺失/非法一律 0（不猜、不抬高水位）。"""

    if not isinstance(event, dict):
        return 0
    for key in ("seq", "sort_seq", "sortSeq"):
        value = event.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def default_event_filter(
    kind: str, event: Any, private_started_at: int = 0
) -> bool:
    """统一的事件过滤：自身回声与启动前的历史私聊只推进水位，不分发。

    为什么私聊要额外挡 create_time：进程启动前就存在的会话水位是"最新一条"，
    但新出现的会话只能取 `seq-1`；这条时间闸门保证任何路径都不会回放旧聊天。
    """

    if not isinstance(event, dict):
        return False
    if event.get("is_self"):
        return False
    if kind == KIND_PRIVATE and private_started_at > 0:
        try:
            created = int(float((event.get("raw") or {}).get("create_time") or 0))
        except (TypeError, ValueError, AttributeError):
            created = 0
        if created and created < private_started_at:
            return False
    return True


class PollTarget:
    """一个被轮询的会话：稳定 ID + 独立水位 + 有界分发队列 + 分发任务。

    `last_seq` 是**读取水位**（事件已入队），`delivered_seq` 是**投递完成水位**
    （上报真的发出去了）。两者分开，才能看清"读到了但还没送出去"的积压。
    """

    __slots__ = (
        "kind", "ref", "name", "last_seq", "delivered_seq",
        "queue", "task", "active", "log_at", "held",
    )

    def __init__(self, kind: str, ref: str, name: str, last_seq: int, queue_max: int) -> None:
        self.kind = kind
        self.ref = ref
        self.name = name
        self.last_seq = int(last_seq)
        # 最后一条**成功上报**事件的 seq（完成水位）。seq 有空洞，不能当条数用。
        self.delivered_seq = int(last_seq)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(queue_max)))
        self.task: Optional[asyncio.Task] = None
        self.active = True
        self.log_at = 0.0
        #: 分发任务当前是否持有 1 条已出队、尚未投递完成的事件（0/1）。
        self.held = 0

    def pending(self) -> int:
        """真实待投递**条数**：队列长度 + 正持有的那条（不是 seq 差）。"""

        return self.backlog() + int(self.held)

    @property
    def key(self) -> tuple:
        return (self.kind, self.ref)

    def backlog(self) -> int:
        return self.queue.qsize()

    def drain(self) -> int:
        """丢弃尚未分发的事件，返回条数（撤销授权/关闭时使用）。"""

        dropped = 0
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break
            dropped += 1
        return dropped

    def __repr__(self) -> str:  # pragma: no cover - 仅日志
        return f"<PollTarget {self.kind}:{self.ref} seq={self.last_seq}>"


class UnifiedPoller:
    """公平轮转读取 + 每会话串行分发；分段耗时与积压只记数字，不含正文。"""

    def __init__(
        self,
        *,
        read: Callable[[str, str, int], Sequence[dict]],
        dispatch: Callable[[PollTarget, dict], Awaitable[None]],
        filter_event: Optional[Callable[[PollTarget, dict], bool]] = None,
        interval: float = 1.5,
        queue_max: int = 200,
        read_batch: int = 50,
        metrics_log_seconds: float = 30.0,
        max_attempts: int = 3,
        retry_base: float = 0.5,
        retry_max: float = 10.0,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._read = read
        self._dispatch = dispatch
        self._filter = filter_event
        self._interval = max(0.05, float(interval))
        self._queue_max = max(1, int(queue_max))
        self._read_batch = max(1, int(read_batch))
        self._metrics_log_seconds = float(metrics_log_seconds or 0)
        # 非传输类失败的最大尝试次数（传输未连接/写帧失败由 retryable 标记，不受限）。
        self._max_attempts = max(1, int(max_attempts or 1))
        self._retry_base = max(0.05, float(retry_base))
        self._retry_max = max(self._retry_base, float(retry_max))
        self._clock = clock
        self._targets: Dict[tuple, PollTarget] = {}
        #: 已撤销会话的分发任务（等当前事件处理完自行退出，关闭时统一等待）。
        self._retired: set = set()
        self._rotation = 0
        self._reader: Optional[asyncio.Task] = None
        self._closed = False
        self._last_metrics_log = 0.0
        #: 按会话类型的投递闸门：sync 不可验证时关闭，恢复后放行（不丢队列）。
        self._gates: Dict[str, asyncio.Event] = {
            KIND_GROUP: asyncio.Event(),
            KIND_PRIVATE: asyncio.Event(),
        }
        for gate in self._gates.values():
            gate.set()
        self._metrics: Dict[str, float] = {
            "rounds": 0,
            "read_calls": 0,
            "read_group": 0,
            "read_private": 0,
            "read_errors": 0,
            "events": 0,
            # 已过滤（自身回声/启动前历史）：只推水位，从不投递。
            "filtered": 0,
            "dispatched": 0,
            "dispatch_errors": 0,
            "dispatch_retry": 0,
            "dispatch_abandoned": 0,
            "backpressure": 0,
            # 待投递事件被丢弃（撤销/等待期间放弃已入队或已持有的那条）。
            "dropped": 0,
            # 读取完成时目标已被撤销：结果从未入队，单独计数。
            "stale": 0,
            "round_ms_total": 0.0,
            "round_ms_max": 0.0,
            "read_ms_total": 0.0,
            "read_ms_max": 0.0,
            "dispatch_ms_total": 0.0,
            "dispatch_ms_max": 0.0,
        }

    # ------------------------------------------------------------------ #
    # 目标管理
    # ------------------------------------------------------------------ #
    def add_target(self, kind: str, ref: str, name: str, last_seq: int) -> PollTarget:
        """登记或刷新一个会话目标。

        已存在的目标只更新显示名，**不重设水位**（水位只能由实际读取推进）。
        """

        if kind not in (KIND_GROUP, KIND_PRIVATE):
            raise ValueError(f"unknown conversation kind: {kind!r}")
        key = (kind, str(ref))
        target = self._targets.get(key)
        if target is not None:
            target.name = name or target.name
            return target
        target = PollTarget(kind, str(ref), name, last_seq, self._queue_max)
        self._targets[key] = target
        if self._reader is not None and not self._reader.done():
            # 运行中新增会话（如刚授权的私聊）：立刻给它一个分发任务。
            self._ensure_dispatcher(target)
        return target

    def has_target(self, kind: str, ref: str) -> bool:
        return (kind, str(ref)) in self._targets

    def get_target(self, kind: str, ref: str) -> Optional[PollTarget]:
        return self._targets.get((kind, str(ref)))

    def targets(self, kind: Optional[str] = None) -> List[PollTarget]:
        return [
            target
            for target in self._targets.values()
            if target.active and (kind is None or target.kind == kind)
        ]

    def remove_target(self, kind: str, ref: str) -> int:
        """移除目标：立即停止读取，丢弃排队事件，**不打断正在执行的分发**。

        为什么不用 cancel：一次分发可能正在做微信 GUI 操作（发消息/发图），
        中途取消会把动作切成"已操作但未确认"的不确定状态。这里改为投递一个
        `None` 哨兵，让分发任务在当前事件结束后自行退出。
        """

        target = self._targets.pop((kind, str(ref)), None)
        if target is None:
            return 0
        target.active = False
        dropped = target.drain()
        self._metrics["dropped"] += dropped
        if target.task is not None and not target.task.done():
            self._retired.add(target.task)
            target.task.add_done_callback(self._retired.discard)
            try:
                target.queue.put_nowait(None)
            except asyncio.QueueFull:  # pragma: no cover - drain 后队列必空
                target.task.cancel()
        target.task = None
        if dropped:
            log.info(
                "[读取] 移除会话 %s:%s，丢弃未分发事件 %d 条",
                kind, target.ref, dropped,
            )
        return dropped

    # ------------------------------------------------------------------ #
    # 投递闸门（权限暂时不可验证时暂停投递，但**不销毁队列与水位**）
    # ------------------------------------------------------------------ #
    def set_paused(self, kind: str, paused: bool) -> None:
        """暂停/放行某一类会话的投递。

        暂停只挡"上报"，读取与入队照常（队列满后自然背压），因此撤销/恢复
        不会丢失已读到的事件，也不会重置水位。
        """

        gate = self._gates.get(kind)
        if gate is None:
            raise ValueError(f"unknown conversation kind: {kind!r}")
        if paused:
            gate.clear()
        else:
            gate.set()

    def is_paused(self, kind: str) -> bool:
        gate = self._gates.get(kind)
        return bool(gate is not None and not gate.is_set())

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("poller already closed")
        if self._reader is not None and not self._reader.done():
            return
        for target in self._targets.values():
            self._ensure_dispatcher(target)
        self._reader = asyncio.create_task(self._read_loop())
        log.info(
            "[读取] 统一轮询启动：会话 %d（群 %d / 私聊 %d），间隔 %.2fs，队列上限 %d",
            len(self.targets()), len(self.targets(KIND_GROUP)),
            len(self.targets(KIND_PRIVATE)), self._interval, self._queue_max,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        dropped = 0
        tasks: List[asyncio.Task] = []
        if self._reader is not None and not self._reader.done():
            tasks.append(self._reader)
        self._reader = None
        for target in self._targets.values():
            target.active = False
            if target.task is not None and not target.task.done():
                tasks.append(target.task)
            target.task = None
            dropped += target.drain()
        tasks.extend(task for task in self._retired if not task.done())
        self._retired.clear()
        self._metrics["dropped"] += dropped
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - 关闭阶段只记类型
                log.warning("[读取] 任务关闭异常：%s", type(exc).__name__)
        self._targets.clear()
        log.info(
            "[读取] 统一轮询已停止：丢弃未分发事件 %d 条；%s",
            dropped, self.metrics_line(),
        )

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def _ensure_dispatcher(self, target: PollTarget) -> None:
        if target.task is not None and not target.task.done():
            return
        target.task = asyncio.create_task(self._dispatch_loop(target))

    async def _read_once(self, target: PollTarget) -> None:
        if not target.active or target.key not in self._targets:
            return  # 读取前已被撤销：不再为它做任何数据库查询
        started = self._clock()
        try:
            events = await asyncio.to_thread(
                self._read, target.kind, target.ref, target.last_seq
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个会话读失败不影响其他会话
            self._metrics["read_errors"] += 1
            log.warning(
                "读取会话失败 kind=%s ref=%s error=%s",
                target.kind, target.ref, type(exc).__name__,
            )
            return
        elapsed = (self._clock() - started) * 1000.0
        self._metrics["read_calls"] += 1
        self._metrics["read_" + target.kind] += 1
        self._metrics["read_ms_total"] += elapsed
        self._metrics["read_ms_max"] = max(self._metrics["read_ms_max"], elapsed)

        if not events:
            return
        if not target.active or target.key not in self._targets:
            # 查询期间被撤销：结果丢弃，不往已取消的队列里塞事件。
            # 这些事件**从未入队**，因此单独计 `stale`，不混进"待投递丢弃"。
            self._metrics["stale"] += len(events)
            return
        queued = 0
        filtered = 0
        for event in list(events)[: self._read_batch]:
            if not isinstance(event, dict):
                continue
            seq = event_seq(event)
            if self._filter is not None and not self._filter(target, event):
                # 自身回声/启动前历史：只推进**读取**水位，从不投递。
                # 不碰 `delivered_seq`（那是"最后一条成功上报"的完成水位），
                # 也不进队列，因此不会把积压算少。
                if seq > target.last_seq:
                    target.last_seq = seq
                filtered += 1
                continue
            try:
                target.queue.put_nowait(event)
            except asyncio.QueueFull:
                # 背压：不推进水位，下一轮从同一位置继续读，保证不丢不重。
                self._metrics["backpressure"] += 1
                log.debug(
                    "[读取] %s:%s 分发队列已满（%d），本轮停止读取",
                    target.kind, target.ref, target.queue.maxsize,
                )
                break
            if seq > target.last_seq:
                target.last_seq = seq
            queued += 1
        self._metrics["events"] += queued
        self._metrics["filtered"] += filtered

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                started = self._clock()
                active = self.targets()
                if active:
                    # 公平轮转：每轮换起点，避免固定群优先、私聊永远在队尾。
                    start = self._rotation % len(active)
                    self._rotation = (start + 1) % len(active)
                    for target in active[start:] + active[:start]:
                        if self._closed:
                            break
                        await self._read_once(target)
                elapsed_ms = (self._clock() - started) * 1000.0
                self._metrics["rounds"] += 1
                self._metrics["round_ms_total"] += elapsed_ms
                self._metrics["round_ms_max"] = max(
                    self._metrics["round_ms_max"], elapsed_ms
                )
                self._maybe_log_metrics()
                await asyncio.sleep(max(0.0, self._interval - elapsed_ms / 1000.0))
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------ #
    # 分发
    # ------------------------------------------------------------------ #
    async def _dispatch_loop(self, target: PollTarget) -> None:
        queue = target.queue
        try:
            while True:
                event = await queue.get()
                if event is None:
                    # 撤销会话的哨兵：当前事件已处理完，正常退出，不打断 GUI 动作。
                    queue.task_done()
                    return
                target.held = 1
                try:
                    await self._deliver(target, event)
                finally:
                    target.held = 0
                    queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _deliver(self, target: PollTarget, event: dict) -> None:
        """投递一条事件，失败**保留这条**并重试，不静默越过。

        - `retryable=True`（反向 WS 未连接/写帧失败）：无限重试，直到恢复；
          期间只等这条，不跳到后面的事件，顺序与幂等由稳定的 `message_id` 保证。
        - 其它异常（媒体解密/构建消息段）：重试 `max_attempts` 次后**明确放弃**
          并计入 `dispatch_abandoned`——否则一条坏媒体会永久堵死整个会话。
        - 会话在等待期间被撤销：不再投递，计入 `dropped`。
        """

        seq = event_seq(event)
        attempt = 0
        # 退避用**已截顶的 delay 递推**，不做无界指数（长断线不会 OverflowError）。
        delay = self._retry_base
        while True:
            await self._gates[target.kind].wait()
            if not target.active or target.key not in self._targets:
                self._metrics["dropped"] += 1
                log.info(
                    "[投递] 会话 %s:%s 已撤销，放弃待投递事件 seq=%s",
                    target.kind, target.ref, seq,
                )
                return
            started = self._clock()
            attempt += 1
            try:
                await self._dispatch(target, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整个会话
                elapsed = (self._clock() - started) * 1000.0
                self._metrics["dispatch_ms_total"] += elapsed
                self._metrics["dispatch_ms_max"] = max(
                    self._metrics["dispatch_ms_max"], elapsed
                )
                self._metrics["dispatch_errors"] += 1
                retryable = bool(getattr(exc, "retryable", False))
                if not retryable and attempt >= self._max_attempts:
                    self._metrics["dispatch_abandoned"] += 1
                    log.error(
                        "[投递] 会话 %s:%s 事件 seq=%s 重试 %d 次仍失败（%s），"
                        "放弃该条并计数（后续事件继续）",
                        target.kind, target.ref, seq, attempt, type(exc).__name__,
                    )
                    return
                self._metrics["dispatch_retry"] += 1
                now = self._clock()
                if now - target.log_at >= 15.0:
                    target.log_at = now
                    log.warning(
                        "[投递] 会话 %s:%s 事件 seq=%s 第 %d 次上报失败（%s，"
                        "retryable=%s），%.1fs 后重试；事件已保留",
                        target.kind, target.ref, seq, attempt,
                        type(exc).__name__, retryable, delay,
                    )
                await asyncio.sleep(delay)
                delay = next_backoff(delay, ceiling=self._retry_max)
                continue
            elapsed = (self._clock() - started) * 1000.0
            self._metrics["dispatch_ms_total"] += elapsed
            self._metrics["dispatch_ms_max"] = max(
                self._metrics["dispatch_ms_max"], elapsed
            )
            self._metrics["dispatched"] += 1
            if seq > target.delivered_seq:
                target.delivered_seq = seq
            return

    # ------------------------------------------------------------------ #
    # 指标（脱敏：只有计数与毫秒）
    # ------------------------------------------------------------------ #
    def metrics(self) -> Dict[str, Any]:
        targets = list(self._targets.values())
        # 积压按**事件条数**统计：队列长度 + 分发任务正持有的那 1 条。
        # 不用 seq 差值（seq 有空洞，差值不等于条数），也不用"读取水位 −
        # 完成水位"（回声被过滤后两者会错位，导致积压被算少）。
        backlog = sum(target.backlog() for target in targets)
        held = sum(int(target.held) for target in targets)
        return {
            **{key: round(value, 1) if isinstance(value, float) else value
               for key, value in self._metrics.items()},
            "targets": len(targets),
            "targets_group": sum(1 for t in targets if t.kind == KIND_GROUP),
            "targets_private": sum(1 for t in targets if t.kind == KIND_PRIVATE),
            "backlog": backlog,
            "backlog_max_one": max((t.backlog() for t in targets), default=0),
            "held": held,
            "pending": backlog + held,
            "pending_max_one": max((t.pending() for t in targets), default=0),
            "paused_group": int(self.is_paused(KIND_GROUP)),
            "paused_private": int(self.is_paused(KIND_PRIVATE)),
        }

    def metrics_line(self) -> str:
        data = self.metrics()
        rounds = max(1, int(data["rounds"]))
        reads = max(1, int(data["read_calls"]))
        dispatched = max(1, int(data["dispatched"]))
        return (
            "poll rounds={rounds} read_calls={read_calls}(group={read_group},private={read_private}) "
            "read_err={read_errors} events={events} filtered={filtered} dispatched={dispatched} "
            "dispatch_err={dispatch_errors} dispatch_retry={dispatch_retry} "
            "abandoned={dispatch_abandoned} dropped={dropped} stale={stale} "
            "pending={pending}/{pending_max_one}(queue={backlog},held={held}) "
            "paused=(g{paused_group},p{paused_private}) "
            "avg_read={avg_read}ms max_read={read_ms_max}ms "
            "avg_round={avg_round}ms max_round={round_ms_max}ms "
            "avg_dispatch={avg_disp}ms max_dispatch={dispatch_ms_max}ms backpressure={backpressure}"
        ).format(
            avg_read=round(data["read_ms_total"] / reads, 1),
            avg_round=round(data["round_ms_total"] / rounds, 1),
            avg_disp=round(data["dispatch_ms_total"] / dispatched, 1),
            **data,
        )

    def _maybe_log_metrics(self) -> None:
        if self._metrics_log_seconds <= 0:
            return
        now = self._clock()
        if now - self._last_metrics_log < self._metrics_log_seconds:
            return
        self._last_metrics_log = now
        log.info("[读取] %s", self.metrics_line())


__all__ = [
    "KIND_GROUP",
    "KIND_PRIVATE",
    "PollTarget",
    "UnifiedPoller",
    "default_event_filter",
    "event_seq",
    "next_backoff",
]
