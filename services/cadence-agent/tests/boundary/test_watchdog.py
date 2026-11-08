"""L3-4 Watchdog：到点只打印该 trace 的协程栈，**不取消**。

如果 watchdog 也负责取消，就会出现两套计时器：watchdog 先取消，`asyncio.timeout`
事后拿到 CancelledError 就不知道是谁取消的，超时归因直接失真。
所以这里三条断言：①到点有栈；②它没有取消任务；③取消时它自己必须上抛。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from cadence.runtime.watchdog import format_task_stack, watch_turn


async def test_watchdog_logs_stack_at_deadline_without_cancelling(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="cadence.watchdog")
    task = asyncio.create_task(asyncio.sleep(1.0), name="slow-turn")

    await asyncio.wait_for(watch_turn(task, trace_id="tr_watch", timeout_s=0.05), timeout=1.0)

    text = caplog.text
    assert "WATCHDOG" in text
    assert "tr_watch" in text, "日志必须能定位到具体 trace"
    assert "协程栈" in text
    assert not task.done(), "watchdog 绝不能取消任务（取消是 asyncio.timeout 的事）"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_watchdog_stays_silent_when_turn_finishes_first(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="cadence.watchdog")
    task = asyncio.create_task(asyncio.sleep(0.01))

    await watch_turn(task, trace_id="tr_fast", timeout_s=0.5)

    assert "WATCHDOG" not in caplog.text
    assert task.done()


async def test_watchdog_cancellation_propagates() -> None:
    """turn 先结束时调用方会 cancel watchdog；它必须把 CancelledError 上抛，不许吞。"""
    task = asyncio.create_task(asyncio.sleep(1.0))
    watcher = asyncio.create_task(watch_turn(task, trace_id="tr_c", timeout_s=5.0))

    await asyncio.sleep(0.01)
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert not task.done(), "取消 watchdog 不能连坐 turn"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_format_task_stack_falls_back_when_no_frames() -> None:
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    text = format_task_stack(task)
    assert text  # 有栈就给栈，没栈给协程 repr —— 反正不能是空串
