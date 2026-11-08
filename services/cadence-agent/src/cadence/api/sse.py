"""SSE 事件流。

**循环不能绑在 HTTP 连接上**（PRD 02 §7.6 B）：

    ✗ 连接断 → 循环被 cancel → 写操作做了一半
    ✓ POST /chat 建循环并返回 stream_id；循环在后台跑，事件进 per-stream 缓冲；
      GET /stream/{id} 只是 tail 这个缓冲；断线重连带 Last-Event-ID 从断点续传

代价是一个内存缓冲，收益是**网络抖动不会让数据停在半修改状态**。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..runtime.events import (  # noqa: F401
    ALL_EVENTS,
    EVENT_CLARIFY,
    EVENT_CONFIRM,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_TOKEN,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
)
from ..runtime.trace import new_stream_id  # noqa: F401


@dataclass
class StreamEvent:
    id: int
    event: str
    data: dict[str, Any]
    at: float = field(default_factory=time.time)

    def frame(self) -> str:
        import json

        payload = json.dumps(self.data, ensure_ascii=False)
        return f"id: {self.id}\nevent: {self.event}\ndata: {payload}\n\n"


class StreamBuffer:
    """单写者队列：append 只由本 turn 的协程调用，tail 只由 SSE 连接调用。

    **为什么不用 `asyncio.Condition`**（L3 铁律）：

    `emit()` 是在**持有会话锁**的情况下调的（整个 turn 都在锁内）。如果 append 需要
    再取一把 Condition 的锁，资源图就多出一条 `会话锁 → 流缓冲锁` 的边 ——
    而 L3 只允许 `会话锁 → LLM 信号量` 这一条单向边。

    去掉锁之后为什么仍然正确：单线程事件循环里，`append` 对 deque + id 的更新之间
    没有 await（原子），`tail` 在 `clear()` 与 `wait()` 之间也没有 await（不会丢失唤醒）。
    """

    def __init__(self, stream_id: str, *, maxlen: int = 512) -> None:
        self.stream_id = stream_id
        self._events: deque[StreamEvent] = deque(maxlen=maxlen)
        self._next_id = 1
        self.closed = False
        self.created_at = time.time()
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------

    @property
    def events(self) -> list[StreamEvent]:
        return list(self._events)

    @property
    def last_id(self) -> int:
        return self._next_id - 1

    async def append(self, event: str, data: dict[str, Any]) -> StreamEvent:
        ev = StreamEvent(id=self._next_id, event=event, data=data)
        self._next_id += 1
        self._events.append(ev)
        self._wake.set()
        return ev

    async def close(self) -> None:
        self.closed = True
        self._wake.set()

    async def tail(self, after_id: int = 0, *, heartbeat_s: float = 15.0) -> AsyncIterator[StreamEvent | None]:
        """从 after_id 之后开始推。返回 None 表示该发心跳了。"""
        cursor = after_id

        while True:
            batch = self._pending(cursor)
            if batch:
                cursor = batch[-1].id
                for ev in batch:
                    yield ev
                continue

            if self.closed:
                return

            # clear 与 wait 之间没有 await：append 不可能在这两步之间插进来，
            # 所以"先检查-再等待"不会丢事件。
            self._wake.clear()
            if self._pending(cursor):
                continue

            try:
                await asyncio.wait_for(self._wake.wait(), timeout=heartbeat_s)
            except (TimeoutError, asyncio.TimeoutError):
                pass

            if not self._pending(cursor) and not self.closed:
                yield None  # 心跳

    def _pending(self, cursor: int) -> list[StreamEvent]:
        return [ev for ev in self._events if ev.id > cursor]


class StreamStore:
    """按 stream_id 管缓冲。带 TTL，避免长跑进程里堆积。"""

    def __init__(self, *, ttl_s: int = 900, max_streams: int = 500, clock=time.time) -> None:
        self.ttl_s = ttl_s
        self.max_streams = max_streams
        self._clock = clock
        self._streams: dict[str, StreamBuffer] = {}

    def create(self, stream_id: str | None = None) -> StreamBuffer:
        sid = stream_id or new_stream_id()
        buf = StreamBuffer(sid)
        self._streams[sid] = buf
        self.sweep()
        return buf

    def get(self, stream_id: str) -> StreamBuffer | None:
        return self._streams.get(stream_id)

    def drop(self, stream_id: str) -> None:
        self._streams.pop(stream_id, None)

    def sweep(self) -> int:
        now = self._clock()
        dead = [
            sid
            for sid, buf in self._streams.items()
            if buf.closed and (now - buf.created_at) > self.ttl_s
        ]
        for sid in dead:
            self._streams.pop(sid, None)
        while len(self._streams) > self.max_streams:
            oldest = min(self._streams.values(), key=lambda b: b.created_at)
            self._streams.pop(oldest.stream_id, None)
        return len(dead)

    def __len__(self) -> int:
        return len(self._streams)


def format_frame(event: StreamEvent) -> str:
    return event.frame()


HEARTBEAT_FRAME = ": ping\n\n"
