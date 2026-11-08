"""L3-3 会话锁：等锁超过 20s → 409 SESSION_BUSY；锁内不再取任何可等待锁。

为什么这条必须有：D2 的默认是"同会话排队"，但**没有上限的排队 = 把用户挂死**。
20s 是"上一条真的卡住了"的判定线。语义上用 409（这个会话忙）而不是 503（整机忙）：
换一个会话完全不受影响，客户端不该因此全局退避。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cadence.deps import set_runtime
from cadence.main import create_app
from cadence.runtime.errors import ErrorCode, ToolError


@pytest.fixture
def client(runtime):
    set_runtime(runtime)
    app = create_app()
    with TestClient(app) as c:
        yield c
    set_runtime(None)


def read_events(client: TestClient, stream_id: str, *, limit: int = 200) -> list[dict]:
    events: list[dict] = []
    current: dict = {}
    with client.stream("GET", f"/stream/{stream_id}") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("event:"):
                current["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                current["data"] = json.loads(line.split(":", 1)[1].strip())
            elif line == "" and current:
                events.append(current)
                if current["event"] == "done":
                    break
                current = {}
            if len(events) > limit:
                break
    return events


class HeldClient:
    """每个回合都慢一点：用来制造"上一条还在处理"的窗口。"""

    name = "held"

    def __init__(self, delay: float = 0.4) -> None:
        self.delay = delay

    async def chat(self, *a, **k):  # pragma: no cover
        raise AssertionError("不该调用")

    async def stream(self, *a, **k):
        import asyncio

        from cadence.llm.base import LLMEvent, LLMResponse

        await asyncio.sleep(self.delay)
        yield LLMEvent(kind="done", response=LLMResponse(text="好的", model="held"))


# ---------------------------------------------------------------------------
# 单元：acquire 超时 → ToolError(SESSION_BUSY)
# ---------------------------------------------------------------------------


async def test_acquire_times_out_with_session_busy(runtime) -> None:
    store = runtime.sessions
    held = store.lock("s_unit")
    await held.acquire()
    try:
        with pytest.raises(ToolError) as exc:
            await store.acquire("s_unit", timeout_s=0.05)
        assert exc.value.code is ErrorCode.SESSION_BUSY
        assert exc.value.recoverable is True, "会话忙是可恢复的：稍后重试或换会话"
        assert store.locked_count() == 1, "超时不能把别人持有的锁放掉"
    finally:
        held.release()

    assert store.locked_count() == 0


async def test_acquire_is_scoped_per_session(runtime) -> None:
    """A 会话忙不影响 B 会话 —— 这是"409 而不是 503"的根本理由。"""
    store = runtime.sessions
    a = store.lock("s_a")
    await a.acquire()
    try:
        b = await store.acquire("s_b", timeout_s=0.05)
        assert b is not None
        b.release()
    finally:
        a.release()


# ---------------------------------------------------------------------------
# HTTP/SSE：第二条消息等锁超时 → error SESSION_BUSY + done(endedBy=busy)
# ---------------------------------------------------------------------------


def test_second_message_on_busy_session_is_reported_as_session_busy(client, runtime) -> None:
    runtime.settings.session_lock_timeout_s = 0.05
    runtime.llm = HeldClient(0.4)
    runtime.loop.llm = runtime.llm
    runtime.loop.fast_llm = runtime.llm

    first = client.post("/chat", json={"sessionId": "s_busy", "message": "第一条"})
    assert first.status_code == 202
    second = client.post("/chat", json={"sessionId": "s_busy", "message": "第二条"})
    assert second.status_code == 202, "第二条被受理，但在锁上等到超时"

    events = read_events(client, second.json()["data"]["streamId"])
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["endedBy"] == "busy"
    assert done["data"]["errorCode"] == "SESSION_BUSY"
    assert done["data"]["suggestions"], "会话忙要给下一步建议"

    errors = [e for e in events if e["event"] == "error"]
    assert errors and errors[0]["data"]["code"] == "SESSION_BUSY"
    assert "会话" in errors[0]["data"]["message"]

    # 第二条没有污染会话：只有第一条的 user turn（等第一条跑完再看）
    read_events(client, first.json()["data"]["streamId"])
    session = runtime.sessions.get("s_busy")
    user_turns = [t for t in session.recent_turns if t.role == "user"]
    assert [t.content for t in user_turns] == ["第一条"]
    assert runtime.sessions.locked_count() == 0, "锁泄漏=0"


def test_session_busy_does_not_block_other_sessions(client, runtime) -> None:
    runtime.settings.session_lock_timeout_s = 0.05
    runtime.llm = HeldClient(0.4)
    runtime.loop.llm = runtime.llm
    runtime.loop.fast_llm = runtime.llm

    first = client.post("/chat", json={"sessionId": "s_slow", "message": "慢的"})
    other = client.post("/chat", json={"sessionId": "s_fast", "message": "别的会话"})
    assert first.status_code == 202 and other.status_code == 202

    other_events = read_events(client, other.json()["data"]["streamId"])
    assert other_events[-1]["data"]["endedBy"] == "finish", "别的会话不该被连累"

    read_events(client, first.json()["data"]["streamId"])
    assert runtime.sessions.locked_count() == 0
