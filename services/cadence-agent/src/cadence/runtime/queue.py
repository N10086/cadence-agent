"""入口准入队列（L3-1）：有界、不排队、满即 503。

**为什么叫"队列"却不排队**：

"排队"解决的是"让请求等着"，但等久了用户只能看到转圈，还会把延迟堆到无限。
这里要的是**背压**：系统最多同时接纳 64 个 turn（排队中 + 运行中），
第 65 个立刻收到 `503 OVERLOADED`，由客户端决定退避 —— 这是可观测、可恢复的失败。

**为什么不用 asyncio.Lock / Semaphore 做准入**：

L3 的铁律是"全进程只有一把可等待锁（会话锁）"。准入判断只读/写一个整数，
中间**没有 await**，在单线程事件循环里天然原子，不需要锁。
真加了锁，反而会多出一条 `会话锁 → 准入锁` 的边（会话锁先拿、turn 里再取别的锁），
把资源图弄出环来 —— 这正是防死锁规则禁止的。

**queue_wait_ms 量的是什么**：

从 `enter()`（被接纳）到 `started()`（turn 协程真正开始跑）之间的时间。
它通常只有零点几毫秒，这本身就是一个结论：50 并发下排队不是瓶颈，
慢在模型或工具（L4 的 T_queue 指标就靠这个数）。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable


class Overloaded(Exception):
    """准入队列已满。调用方应转成 503 OVERLOADED，而不是继续等。"""


@dataclass(frozen=True)
class QueueStats:
    capacity: int
    inflight: int
    max_inflight: int
    admitted_total: int
    rejected_total: int
    wait_p50_ms: float
    wait_p95_ms: float

    @property
    def rejected_ratio(self) -> float:
        total = self.admitted_total + self.rejected_total
        return (self.rejected_total / total) if total else 0.0


class TurnQueue:
    """按 turn_id 记账的准入控制。所有方法都是同步的，不会阻塞事件循环。"""

    def __init__(
        self,
        capacity: int = 64,
        *,
        clock: Callable[[], float] = time.perf_counter,
        sample_size: int = 4096,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity 必须 >= 1")
        self.capacity = capacity
        self._clock = clock
        self._entered: dict[str, float] = {}
        self._inflight = 0
        self._max_inflight = 0
        self._admitted = 0
        self._rejected = 0
        self._waits_ms: deque[float] = deque(maxlen=sample_size)

    # ------------------------------------------------------------------

    def enter(self, turn_id: str) -> None:
        """占一个名额。满了抛 `Overloaded` —— 绝不等待。"""
        if self._inflight >= self.capacity:
            self._rejected += 1
            raise Overloaded(
                f"服务繁忙：同时在处理的请求已达上限 {self.capacity}，请稍后重试"
            )
        self._inflight += 1
        self._admitted += 1
        self._max_inflight = max(self._max_inflight, self._inflight)
        self._entered[turn_id] = self._clock()

    def started(self, turn_id: str) -> None:
        """turn 协程真正开始执行。记录它等了多久（通常 <1ms）。"""
        entered_at = self._entered.get(turn_id)
        if entered_at is None:
            return
        self._waits_ms.append(max(0.0, (self._clock() - entered_at) * 1000.0))

    def leave(self, turn_id: str) -> None:
        """归还名额。必须放在 turn 的 finally 里：异常/取消都不能漏。"""
        if self._entered.pop(turn_id, None) is None:
            # 没 enter 过（或重复 leave）：不静默改计数，避免把别的 turn 的名额放掉
            return
        self._inflight = max(0, self._inflight - 1)

    # ------------------------------------------------------------------

    @property
    def inflight(self) -> int:
        return self._inflight

    def wait_samples(self) -> list[float]:
        """原始排队样本（毫秒）。压测脚本要把多个场景的样本合并后再算 p50/p95。"""
        return list(self._waits_ms)

    def stats(self) -> QueueStats:
        waits = sorted(self._waits_ms)
        return QueueStats(
            capacity=self.capacity,
            inflight=self._inflight,
            max_inflight=self._max_inflight,
            admitted_total=self._admitted,
            rejected_total=self._rejected,
            wait_p50_ms=_percentile(waits, 50),
            wait_p95_ms=_percentile(waits, 95),
        )


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return round(sorted_values[0], 3)
    k = (len(sorted_values) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    value = sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac
    return round(value, 3)
