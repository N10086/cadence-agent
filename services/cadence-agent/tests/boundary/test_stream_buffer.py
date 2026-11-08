"""SSE 流缓冲：无锁单写者队列的行为回归（去 Condition 之后必须仍然正确）。

去掉锁之后最容易出的两类 bug：
1. **丢唤醒**：tail 刚判断"没事件"就去等，而 append 正好在 clear 与 wait 之间插入。
   （单线程里 clear 和 wait 之间没有 await，所以不会发生 —— 这条测试守着它。）
2. **关流不唤醒**：close 之后 tail 永远挂着，前端看到空白页而不是结束。
"""

from __future__ import annotations

import asyncio

from cadence.api.sse import StreamBuffer


async def _drain(buf: StreamBuffer, after: int = 0) -> list:
    out = []
    async for ev in buf.tail(after, heartbeat_s=0.05):
        if ev is None:
            continue
        out.append(ev)
    return out


async def test_tail_sees_events_appended_after_it_starts() -> None:
    buf = StreamBuffer("st_live")
    consumer = asyncio.create_task(_drain(buf))
    await asyncio.sleep(0)  # 让 tail 走到 wait

    await buf.append("token", {"text": "你"})
    await buf.append("token", {"text": "好"})
    await buf.append("done", {"traceId": "tr_1"})
    await buf.close()

    events = await asyncio.wait_for(consumer, timeout=1.0)
    assert [e.event for e in events] == ["token", "token", "done"]
    assert [e.data["text"] for e in events[:2]] == ["你", "好"]
    assert [e.id for e in events] == [1, 2, 3]


async def test_tail_resumes_from_last_event_id() -> None:
    buf = StreamBuffer("st_resume")
    for i in range(3):
        await buf.append("token", {"text": str(i)})
    await buf.close()

    events = await asyncio.wait_for(_drain(buf, after=2), timeout=1.0)
    assert [e.id for e in events] == [3]


async def test_close_wakes_a_waiting_tail() -> None:
    buf = StreamBuffer("st_close")
    consumer = asyncio.create_task(_drain(buf))
    await asyncio.sleep(0.02)

    await buf.close()
    assert await asyncio.wait_for(consumer, timeout=1.0) == []


async def test_heartbeat_is_emitted_when_idle() -> None:
    buf = StreamBuffer("st_hb")
    consumer = asyncio.create_task(_drain_until_heartbeat(buf))
    try:
        heartbeat = await asyncio.wait_for(consumer, timeout=1.0)
        assert heartbeat is None
    finally:
        await buf.close()


async def _drain_until_heartbeat(buf: StreamBuffer):
    async for ev in buf.tail(heartbeat_s=0.01):
        return ev
    return "closed"


async def test_new_append_wakes_tail_after_a_heartbeat() -> None:
    """心跳之后仍有事件时不能"睡死"：这是 clear() 之后必须重查 pending 的原因。"""
    buf = StreamBuffer("st_after_hb")
    collected: list = []

    async def consume() -> None:
        async for ev in buf.tail(heartbeat_s=0.01):
            if ev is None:
                continue
            collected.append(ev)
            if ev.event == "done":
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # 至少经历一次心跳
    await buf.append("done", {"traceId": "tr_2"})
    await asyncio.wait_for(task, timeout=1.0)

    assert [e.event for e in collected] == ["done"]
