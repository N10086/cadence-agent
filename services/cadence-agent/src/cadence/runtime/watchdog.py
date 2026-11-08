"""看门狗（L3-4）：**只打印协程栈，不取消**。

取消由 `api/chat.py` 的 `asyncio.timeout(turn_timeout_s)` 负责。为什么必须分开：

- 两套超时计时器会互相打架：watchdog 先取消，`asyncio.timeout` 事后拿到
  `CancelledError` 就不知道"到底是谁取消的"，超时归因会变成一团乱麻。
- 排查卡死需要的是**到点那一刻的栈**；而取消会把栈展开掉。所以：
  watchdog 到点只抓栈写日志，随后 `asyncio.timeout` 正常取消并走 L2 的超时收尾。

实现上有一个容易踩的坑：`asyncio.wait_for(task)` 超时时会**取消 task**。
这里必须 `asyncio.shield(task)`，否则 watchdog 就成了第二套取消器。
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any


def format_task_stack(task: asyncio.Task[Any]) -> str:
    """把某任务当前挂起的位置格式化成可读栈。空栈时退回协程 repr，避免"打印了个寂寞"。"""
    frames = task.get_stack()
    if not frames:
        return f"<无栈帧> coro={task.get_coro()!r}"
    # format_stack(frame) 返回 list[str]，要逐帧摊平
    return "".join(line for frame in frames for line in traceback.format_stack(frame))


async def watch_turn(
    task: asyncio.Task[Any],
    *,
    trace_id: str,
    timeout_s: float,
    logger: logging.Logger | None = None,
) -> None:
    """等 `task` 结束；到点还没结束就把它的协程栈打出来。**绝不取消它。**"""
    log = logger or logging.getLogger("cadence.watchdog")
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
    except TimeoutError:
        stack = format_task_stack(task)
        others = len([t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
        log.error(
            "WATCHDOG trace=%s 超过 %.1fs 仍未结束（task_done=%s，同时存活任务=%d）；协程栈：\n%s",
            trace_id,
            timeout_s,
            task.done(),
            others,
            stack,
        )
    except asyncio.CancelledError:
        # turn 先结束了 → 调用方 cancel 了 watchdog。取消必须上抛，不许吞。
        raise
