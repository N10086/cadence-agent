"""沙箱：超时 / 并发上限 / 按工具熔断。"""

from __future__ import annotations

import asyncio

import pytest

from cadence.runtime.errors import ErrorCode, ToolError
from cadence.runtime.sandbox import SandboxRejected, SandboxTimeout, ToolSandbox


async def test_timeout_raises_and_records() -> None:
    sandbox = ToolSandbox(timeout_s=0.01)

    async def slow() -> str:
        await asyncio.sleep(0.2)
        return "done"

    with pytest.raises(SandboxTimeout):
        await sandbox.run("task.search", slow)

    snap = sandbox.snapshot()["task.search"]
    assert snap["timeouts"] == 1
    assert snap["failed"] == 1


async def test_success_resets_failure_count() -> None:
    sandbox = ToolSandbox(breaker_threshold=3)

    async def boom() -> None:
        raise ToolError(ErrorCode.VALIDATION_ERROR, "bad args")

    async def ok() -> str:
        return "ok"

    for _ in range(2):
        with pytest.raises(ToolError):
            await sandbox.run("task.update", boom)
    assert sandbox.snapshot()["task.update"]["failures"] == 2

    await sandbox.run("task.update", ok)
    assert sandbox.snapshot()["task.update"]["failures"] == 0


async def test_breaker_opens_after_threshold_and_recovers_after_cooldown() -> None:
    now = [1000.0]
    sandbox = ToolSandbox(breaker_threshold=2, breaker_cooldown_s=30.0, clock=lambda: now[0])

    async def boom() -> None:
        raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "core down")

    async def ok() -> str:
        return "ok"

    for _ in range(2):
        with pytest.raises(ToolError):
            await sandbox.run("task.delete", boom)

    # 第三次直接被短路，不再打下游
    with pytest.raises(SandboxRejected) as exc:
        await sandbox.run("task.delete", ok)
    assert exc.value.retry_after_s > 0

    # cooldown 到期后放行，且成功会清零
    now[0] += 31
    assert await sandbox.run("task.delete", ok) == "ok"
    assert sandbox.snapshot()["task.delete"]["failures"] == 0


async def test_breaker_is_per_tool() -> None:
    """一个工具熔断不影响另一个：LLM 挂了和 task.search 挂了是两件事。"""
    sandbox = ToolSandbox(breaker_threshold=1, breaker_cooldown_s=60.0)

    async def boom() -> None:
        raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "down")

    async def ok() -> str:
        return "ok"

    with pytest.raises(ToolError):
        await sandbox.run("schedule.busy", boom)

    with pytest.raises(SandboxRejected):
        await sandbox.run("schedule.busy", ok)

    # 另一个工具照常
    assert await sandbox.run("task.search", ok) == "ok"


async def test_concurrency_cap_serializes_same_tool() -> None:
    sandbox = ToolSandbox(max_concurrency=1)
    active = 0
    peak = 0

    async def job() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    await asyncio.gather(*[sandbox.run("task.search", job) for _ in range(4)])
    assert peak == 1, "同一工具并发被上限压住"


async def test_snapshot_reports_health() -> None:
    sandbox = ToolSandbox(breaker_threshold=1)

    async def boom() -> None:
        raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "x")

    with pytest.raises(ToolError):
        await sandbox.run("plan.apply", boom)

    snap = sandbox.snapshot()["plan.apply"]
    assert snap["total"] == 1
    assert snap["failed"] == 1
    assert snap["open"] is True
    assert "x" in snap["last_error"]
