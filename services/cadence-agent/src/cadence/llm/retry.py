"""重试与熔断。

一条原则（ADR D7）：**熔断给外部依赖，重试给内部依赖。**
LLM provider：熔断 + 降级。Core：只做超时 + 有限重试，不熔断。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .base import LLMBreakerOpen, LLMError, LLMRateLimited

T = TypeVar("T")


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
    """0.5 / 1 / 2 / 4 ... 上限 cap，加 0~0.5s 抖动防止同步重试打爆 provider。"""
    raw = min(base * (2**attempt), cap)
    return raw + random.uniform(0, 0.5)


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    times: int = 3,
    base_delay: float = 0.5,
    should_retry: Callable[[Exception], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """重试 `times` 次（不含首次）。

    `should_retry` 返回 False 的错误立刻上抛 —— 这是 recoverable=false 的执行点，
    也是"防止死循环烧钱"的最后一道闸。
    """
    last: Exception | None = None
    for attempt in range(times + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - 逐层判断是否可重试
            last = exc
            if attempt >= times:
                break
            if should_retry is not None and not should_retry(exc):
                raise
            delay = getattr(exc, "retry_after_s", None) or backoff_delay(attempt, base_delay)
            await sleep(float(delay))
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"


class Breaker:
    """按 provider 分别实例化。滑动窗口统计失败率。

    OPEN 期间**不打 provider** —— 否则会在它已经限流的时候继续加压。
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: float = 0.5,
        window: int = 20,
        cooldown_s: float = 30.0,
        cooldown_max_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.window = window
        self.cooldown_s = cooldown_s
        self.cooldown_max_s = cooldown_max_s
        self._clock = clock
        self._results: deque[bool] = deque(maxlen=window)
        self._state = CLOSED
        self._opened_at = 0.0
        self._cooldown = cooldown_s
        self.open_count = 0

    @property
    def state(self) -> str:
        self._maybe_half_open()
        return self._state

    @property
    def cooldown_current(self) -> float:
        """下一次 OPEN 会用的冷却时长（只读，测试与 /health 用来看退避到哪一档了）。"""
        return min(self.cooldown_s * (2 ** max(0, self.open_count - 1)), self.cooldown_max_s)

    def _maybe_half_open(self) -> None:
        if self._state == OPEN and self._clock() - self._opened_at >= self._cooldown:
            self._state = HALF_OPEN

    def allow(self) -> bool:
        self._maybe_half_open()
        return self._state != OPEN

    def record(self, ok: bool) -> None:
        if self._state == HALF_OPEN:
            if ok:
                self._reset()
            else:
                self._open()
            return

        self._results.append(ok)
        if self._state == CLOSED and self._failure_rate() >= self.failure_threshold:
            if len(self._results) >= min(self.window, 4):
                self._open()

    def _failure_rate(self) -> float:
        if not self._results:
            return 0.0
        return sum(1 for r in self._results if not r) / len(self._results)

    def _open(self) -> None:
        """打开熔断。**首次冷却就是 cooldown_s（30s），不是 60s。**

        退避档位由 open_count 的序号决定：第 1 次 OPEN = 30s、第 2 次 = 60s、
        第 3 次 = 120s …… 封顶 300s。`_reset()` 把 open_count 清零，所以一次成功
        探测之后的下一轮故障又从 30s 重新开始退避。

        旧实现是在 `_open()` 里直接 `cooldown *= 2`，导致**第一次打开就等 60s**：
        provider 只是抖了一下，用户却要等一分钟。契约（L2-1）明确要求先 30s。
        """
        self._state = OPEN
        self._opened_at = self._clock()
        self.open_count += 1
        self._cooldown = self.cooldown_current

    def _reset(self) -> None:
        self._state = CLOSED
        self._results.clear()
        self._cooldown = self.cooldown_s
        self.open_count = 0

    def guard(self) -> None:
        if not self.allow():
            raise LLMBreakerOpen(f"{self.name} 熔断器打开，本轮不发起请求")


# ---------------------------------------------------------------------------
# 熔断 + 重试的组合包装
# ---------------------------------------------------------------------------


async def call_with_resilience(
    fn: Callable[[], Awaitable[T]],
    *,
    breaker: Breaker | None = None,
    times: int = 1,
    base_delay: float = 0.5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """先查熔断，再重试，每次结果回写熔断器。

    `guard()` 放在 `once()` **内部**：重试过程中熔断器可能因为前面几次失败刚打开，
    这时必须立刻停手。只在外层查一次的话，后面的重试仍会打 provider ——
    那正是「OPEN 期间不打 provider」要禁止的事。
    """

    async def once() -> T:
        if breaker is not None:
            breaker.guard()
        try:
            result = await fn()
        except LLMError as exc:
            if breaker is not None and not isinstance(exc, LLMBreakerOpen):
                breaker.record(ok=False)
            raise
        except Exception:
            if breaker is not None:
                breaker.record(ok=False)
            raise
        if breaker is not None:
            breaker.record(ok=True)
        return result

    def _should_retry(exc: Exception) -> bool:
        if isinstance(exc, LLMBreakerOpen):
            return False
        # 429 不再是"重试"的理由：provider 已经明确说别打了，重试只会更糟，
        # 而且会把一次限流放大成 N 次计费。429 交给上行做降级（L2-2）。
        if isinstance(exc, LLMRateLimited):
            return False
        return getattr(exc, "retryable", False)

    return await retry_async(once, times=times, base_delay=base_delay, should_retry=_should_retry, sleep=sleep)
