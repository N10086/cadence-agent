"""L3-1 入口准入队列：有界、满即 503、不排队。

"队列"这个词容易误导：这里要的不是"让请求排队等"，而是**背压**。
第 65 个请求拿到的是可恢复的 503，而不是一个会一直转圈的页面。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from cadence.api.sse import StreamBuffer
from cadence.deps import set_runtime
from cadence.main import create_app
from cadence.runtime.queue import Overloaded, TurnQueue


# ---------------------------------------------------------------------------
# 纯队列行为
# ---------------------------------------------------------------------------


def test_queue_rejects_when_full_and_never_blocks() -> None:
    q = TurnQueue(capacity=2)
    q.enter("t1")
    q.enter("t2")

    started = time.perf_counter()
    with pytest.raises(Overloaded):
        q.enter("t3")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.01, "满了必须立刻拒绝，不许等"
    stats = q.stats()
    assert stats.inflight == 2
    assert stats.rejected_total == 1
    assert stats.rejected_ratio == pytest.approx(1 / 3)


def test_queue_leave_frees_capacity_and_is_idempotent() -> None:
    q = TurnQueue(capacity=1)
    q.enter("t1")
    with pytest.raises(Overloaded):
        q.enter("t2")

    q.leave("t1")
    q.leave("t1")  # 重复 leave 不能把 inflight 减成负数
    assert q.stats().inflight == 0

    q.enter("t3")
    assert q.stats().inflight == 1


def test_queue_wait_is_measured_from_enter_to_started() -> None:
    now = [0.0]
    q = TurnQueue(capacity=4, clock=lambda: now[0])
    q.enter("t1")
    now[0] = 0.012  # 12ms：模拟被事件循环调度前的等待
    q.started("t1")

    stats = q.stats()
    assert stats.wait_p50_ms == pytest.approx(12.0, abs=0.01)
    assert stats.wait_p95_ms == pytest.approx(12.0, abs=0.01)
    assert stats.max_inflight == 1


def test_queue_capacity_is_configurable_from_settings() -> None:
    from cadence.config import Settings

    s = Settings(llm_mode="fake", deepseek_api_key="")
    assert s.turn_queue_capacity == 64, "契约：有界队列 64"


# ---------------------------------------------------------------------------
# HTTP：满了必须 503 OVERLOADED（而不是 202 之后卡住）
# ---------------------------------------------------------------------------


class SlowClient:
    name = "slow"

    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay

    async def chat(self, *a, **k):  # pragma: no cover
        raise AssertionError("不该调用")

    async def stream(self, *a, **k):
        import asyncio

        from cadence.llm.base import LLMEvent, LLMResponse

        await asyncio.sleep(self.delay)
        yield LLMEvent(kind="done", response=LLMResponse(text="好", model="slow"))


@pytest.fixture
def client(runtime):
    set_runtime(runtime)
    app = create_app()
    with TestClient(app) as c:
        yield c
    set_runtime(None)


def read_events(client: TestClient, stream_id: str, *, limit: int = 200) -> list[dict]:
    import json

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


def test_chat_returns_503_overloaded_when_queue_full(client, runtime) -> None:
    runtime.queue.capacity = 1
    runtime.llm = SlowClient(0.3)
    runtime.loop.llm = runtime.llm
    runtime.loop.fast_llm = runtime.llm

    first = client.post("/chat", json={"sessionId": "s_q1", "message": "看看任务"})
    assert first.status_code == 202

    second = client.post("/chat", json={"sessionId": "s_q2", "message": "看看任务"})
    assert second.status_code == 503
    body = second.json()
    assert body["code"] == 503
    assert body["data"]["errorCode"] == "OVERLOADED"
    assert "繁忙" in body["message"]

    # 第一轮结束后名额归还，第三个请求能进
    read_events(client, first.json()["data"]["streamId"])
    assert runtime.queue.inflight == 0
    third = client.post("/chat", json={"sessionId": "s_q3", "message": "看看任务"})
    assert third.status_code == 202
    read_events(client, third.json()["data"]["streamId"])


# ---------------------------------------------------------------------------
# 铁律：准入队列不许引入第二把可等待锁
# ---------------------------------------------------------------------------


def test_queue_module_has_no_waitable_lock() -> None:
    import ast

    from cadence.runtime import queue as queue_mod

    source = open(queue_mod.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    forbidden = {"Lock", "Semaphore", "Condition", "Event"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            raise AssertionError(f"准入队列里不允许出现 asyncio.{node.attr}")
    assert not hasattr(TurnQueue(2), "__aenter__")


def test_stream_buffer_has_no_condition() -> None:
    """流缓冲改用 Event：否则 emit() 会在持有会话锁时再取一把锁（嵌套锁）。"""
    buf = StreamBuffer("st_x")
    assert not hasattr(buf, "cond")
