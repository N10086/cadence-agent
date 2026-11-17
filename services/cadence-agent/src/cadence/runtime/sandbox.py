"""工具执行沙箱（进程内执行治理层）。

**先划清边界**：这里不是 OS 级隔离（seccomp / 容器 / 独立进程）。本项目的工具是
"Agent 进程内的 async 函数，通过 httpx 调 Core REST"，不是任意代码执行器，所以
真正的沙箱收益有限；企业里最常被追问的是「工具失控会不会拖垮整轮 / 打爆下游」，
那属于**执行治理**，由这里负责：

1. **单工具超时**：一个工具 hang 住不能拖死整轮（此前只有 LLM 读超时）；
2. **按工具并发上限**：同一个工具最多 N 个并发，避免瞬时打爆 Core；
3. **按工具熔断**：连续失败 N 次后短路 cooldown 秒，不再打下游
   （与 LLM 熔断分开：LLM 挂了不代表 task.delete 挂了，反之亦然）；
4. **失败计数与状态快照**：进 trace / 健康检查，能回答"哪个工具在拖后腿"。

能做的隔离仍保留在护栏层：`FORBIDDEN_TOOLS` 禁止危险工具、权限四类、
危险操作两阶段确认。**这三层加起来，才等于"工具不会越权、不会拖垮、不会打爆"。**
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..runtime.errors import ErrorCode, ToolError


class SandboxRejected(Exception):
    """工具被沙箱拒绝（熔断打开 / 并发打满到超时）。"""

    def __init__(self, tool: str, message: str, *, retry_after_s: float = 0.0) -> None:
        super().__init__(message)
        self.tool = tool
        self.message = message
        self.retry_after_s = retry_after_s


@dataclass
class _ToolState:
    failures: int = 0
    open_until: float = 0.0
    total: int = 0
    failed: int = 0
    timeouts: int = 0
    #: 最近一次错误，便于 health / trace 展示
    last_error: str = ""


@dataclass
class ToolSandbox:
    """按工具的超时 / 并发 / 熔断。全部状态在事件循环内，无需加锁。"""

    timeout_s: float = 10.0
    max_concurrency: int = 8
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 30.0
    clock: Callable[[], float] = time.monotonic

    _states: dict[str, _ToolState] = field(default_factory=dict)
    _semaphores: dict[str, asyncio.Semaphore] = field(default_factory=dict)

    # ------------------------------------------------------------------

    def _state(self, tool: str) -> _ToolState:
        return self._states.setdefault(tool, _ToolState())

    def _sem(self, tool: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(tool)
        if sem is None:
            sem = asyncio.Semaphore(self.max_concurrency)
            self._semaphores[tool] = sem
        return sem

    def _check_breaker(self, tool: str) -> None:
        state = self._state(tool)
        now = self.clock()
        if state.open_until <= now:
            # cooldown 到期：半开，允许下一次尝试；成功会清零，失败会重新打开
            if state.open_until:
                state.open_until = 0.0
                state.failures = 0
            return
        raise SandboxRejected(
            tool,
            f"工具 {tool} 连续失败 {state.failures} 次，已熔断，{state.open_until - now:.0f}s 后重试",
            retry_after_s=state.open_until - now,
        )

    def _record(self, tool: str, *, ok: bool, timed_out: bool = False, error: str = "") -> None:
        state = self._state(tool)
        state.total += 1
        if ok:
            state.failures = 0
            return
        state.failed += 1
        if timed_out:
            state.timeouts += 1
        state.failures += 1
        state.last_error = error[:200]
        if state.failures >= self.breaker_threshold:
            state.open_until = self.clock() + self.breaker_cooldown_s

    # ------------------------------------------------------------------

    async def run(
        self,
        tool: str,
        factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        """在沙箱里执行一次工具调用。

        抛 `SandboxRejected`（熔断）或 `SandboxTimeout`（超时）；工具自身的
        `ToolError` 原样透传（由调用方决定是否算失败）。
        """
        self._check_breaker(tool)
        async with self._sem(tool):
            try:
                result = await asyncio.wait_for(factory(), timeout=self.timeout_s)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                self._record(tool, ok=False, timed_out=True, error="timeout")
                raise SandboxTimeout(
                    tool, f"工具 {tool} 执行超时（>{self.timeout_s:.0f}s）"
                ) from exc
            except ToolError as exc:
                # 业务错误（400/404/409）不算"下游挂了"，但仍累计失败：
                # 连续 3 次同类错误说明模型在乱调，短路它能省下两轮。
                self._record(tool, ok=False, error=exc.message)
                raise
            except Exception as exc:  # noqa: BLE001
                self._record(tool, ok=False, error=f"{type(exc).__name__}: {exc}")
                raise
            self._record(tool, ok=True)
            return result

    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = self.clock()
        return {
            name: {
                "total": s.total,
                "failed": s.failed,
                "timeouts": s.timeouts,
                "failures": s.failures,
                "open": s.open_until > now,
                "open_for_s": round(max(0.0, s.open_until - now), 1),
                "last_error": s.last_error,
            }
            for name, s in self._states.items()
        }


class SandboxTimeout(Exception):
    """工具执行超时（沙箱层）。"""

    def __init__(self, tool: str, message: str) -> None:
        super().__init__(message)
        self.tool = tool
        self.message = message

    def as_tool_error(self) -> ToolError:
        return ToolError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            self.message,
            core_down=False,
        )
