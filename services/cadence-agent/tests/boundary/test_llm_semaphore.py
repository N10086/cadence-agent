"""L3-2 全局 LLM 信号量：16 并发上限，且只在 provider 调用点持有。

为什么必须是"叶子"：会话锁 → LLM 信号量 是唯一允许的边。如果任何代码在持有
信号量时去拿会话锁（或任何别的可等待对象），资源图就成环了 —— 那是死锁。
所以这里除了断言并发上限，还断言**取消能归还名额**（否则一轮被取消就把名额漏光）。
"""

from __future__ import annotations

import asyncio

import pytest

from cadence.llm.base import LLMEvent, LLMResponse
from cadence.runtime.loop import Loop

from conftest import make_ctx


class CountingClient:
    """记录同时在飞的 provider 调用数。"""

    name = "counting"

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.total = 0

    async def chat(self, *a, **k):  # pragma: no cover
        raise AssertionError("不该调用")

    async def stream(self, *a, **k):
        self.total += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.active -= 1
        yield LLMEvent(kind="done", response=LLMResponse(text="ok", model="counting"))


def _loop(runtime, client, sem) -> Loop:
    return Loop(
        settings=runtime.settings,
        registry=runtime.registry,
        llm=client,
        llm_semaphore=sem,
    )


async def _one_call(loop: Loop, client, ctx, events: list) -> LLMResponse:
    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    return await loop._call_llm(client, [{"role": "user", "content": "hi"}], ctx, emit, "fast")


async def test_semaphore_caps_concurrent_provider_calls(runtime) -> None:
    client = CountingClient(0.02)
    loop = _loop(runtime, client, asyncio.Semaphore(2))
    ctx = make_ctx(runtime)

    results = await asyncio.gather(*[_one_call(loop, client, ctx, []) for _ in range(8)])

    assert len(results) == 8
    assert client.total == 8
    assert client.max_active <= 2, f"并发超过信号量上限：{client.max_active}"
    assert client.max_active >= 2, "信号量应该真的让两个调用同时进行（否则是串行）"


async def test_without_semaphore_calls_are_unbounded(runtime) -> None:
    """反证：把信号量拿掉，确实是 8 个同时在飞 —— 限制来自信号量，不是 client 自己。"""
    client = CountingClient(0.02)
    loop = _loop(runtime, client, None)
    ctx = make_ctx(runtime)

    await asyncio.gather(*[_one_call(loop, client, ctx, []) for _ in range(8)])
    assert client.max_active == 8


async def test_cancelled_call_releases_the_semaphore(runtime) -> None:
    client = CountingClient(1.0)
    sem = asyncio.Semaphore(1)
    loop = _loop(runtime, client, sem)
    ctx = make_ctx(runtime)

    task = asyncio.create_task(_one_call(loop, client, ctx, []))
    await asyncio.sleep(0.05)
    assert sem.locked(), "慢调用期间名额应被占住"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 名额必须归还：否则一次取消就把 16 个槽位之一永久漏掉
    await asyncio.wait_for(sem.acquire(), timeout=0.2)
    sem.release()


async def test_semaphore_is_created_once_per_runtime_and_configurable(runtime) -> None:
    from cadence.config import Settings

    s = Settings(llm_mode="fake", deepseek_api_key="")
    assert s.llm_max_concurrency == 16, "契约：全局信号量 16"
    assert isinstance(runtime.llm_semaphore, asyncio.Semaphore)
