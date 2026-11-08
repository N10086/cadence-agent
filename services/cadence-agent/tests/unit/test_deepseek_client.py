"""OpenAI-compatible 客户端：流式拼接与错误映射。

这是整个 LLM 层最容易出错的地方：
**流式 tool_call 的 arguments 是分片返回的**，拼错一个字符就是非法 JSON。
用 MockTransport 把分片原样喂进来测。
"""

from __future__ import annotations

import json

import httpx
import pytest

from cadence.llm.base import (
    LLMBadOutput,
    LLMBadRequest,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)
from cadence.llm.deepseek import DeepSeekClient


def make_client(handler) -> DeepSeekClient:
    http = httpx.AsyncClient(
        base_url="https://api.deepseek.com/v1",
        transport=httpx.MockTransport(handler),
    )
    return DeepSeekClient(api_key="sk-test", base_url="https://api.deepseek.com/v1", model="deepseek-chat", http=http)


def sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def delta(content: str | None = None, tool_calls: list[dict] | None = None, finish: str | None = None) -> dict:
    d: dict = {}
    if content is not None:
        d["content"] = content
    if tool_calls is not None:
        d["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "delta": d, "finish_reason": finish}], "model": "deepseek-chat"}


async def collect(client: DeepSeekClient, messages=None) -> list:
    return [ev async for ev in client.stream(messages or [{"role": "user", "content": "hi"}])]


# ---------------------------------------------------------------------------
# 非流式
# ---------------------------------------------------------------------------


async def test_chat_parses_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {"name": "task_search", "arguments": '{"limit": 5}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    resp = await make_client(handler).chat([{"role": "user", "content": "hi"}], tools=[])
    assert resp.tool_calls[0].name == "task_search"
    assert resp.tool_calls[0].arguments == {"limit": 5}
    assert resp.tokens_in == 100 and resp.tokens_out == 20


async def test_chat_captures_reasoning_separately() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "答案", "reasoning_content": "让我想想"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    resp = await make_client(handler).chat([{"role": "user", "content": "hi"}])
    assert resp.reasoning == "让我想想"
    assert resp.text == "答案"


# ---------------------------------------------------------------------------
# 流式
# ---------------------------------------------------------------------------


async def test_stream_assembles_fragmented_tool_arguments() -> None:
    """真实 provider 会把 arguments 拆成几片发，必须按 index 累加。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                delta(tool_calls=[{"index": 0, "id": "call_1", "function": {"name": "task_search", "arguments": '{"key'}}]),
                delta(tool_calls=[{"index": 0, "function": {"arguments": 'word": "牛奶", '}}]),
                delta(tool_calls=[{"index": 0, "function": {"arguments": '"limit": 5}'}}]),
                delta(finish="tool_calls"),
                {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 10}, "model": "deepseek-chat"},
            ),
        )

    events = await collect(make_client(handler))
    done = events[-1]
    assert done.kind == "done"
    assert done.response is not None
    call = done.response.tool_calls[0]
    assert call.name == "task_search"
    assert call.arguments == {"keyword": "牛奶", "limit": 5}
    assert done.response.tokens_in == 50


async def test_stream_emits_text_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(delta("你"), delta("好"), delta(finish="stop")),
        )

    events = await collect(make_client(handler))
    assert "".join(e.text for e in events if e.kind == "token") == "你好"


async def test_stream_bad_arguments_raises_bad_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                delta(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "task_search", "arguments": "{bad"}}]),
                delta(finish="tool_calls"),
            ),
        )

    with pytest.raises(LLMBadOutput):
        await collect(make_client(handler))


async def test_truncated_stream_is_rejected() -> None:
    """半截响应必须丢弃 —— 拿去执行比不执行危险得多。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse(delta(finish="length")))

    with pytest.raises(LLMBadOutput):
        await collect(make_client(handler))


# ---------------------------------------------------------------------------
# 错误映射
# ---------------------------------------------------------------------------


async def test_429_maps_to_rate_limited_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "slow down"})

    with pytest.raises(LLMRateLimited) as exc:
        await make_client(handler).chat([{"role": "user", "content": "hi"}])
    assert exc.value.retry_after_s == 2.0


async def test_401_maps_to_bad_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(LLMBadRequest):
        await make_client(handler).chat([{"role": "user", "content": "hi"}])


async def test_500_maps_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    with pytest.raises(LLMUnavailable):
        await make_client(handler).chat([{"role": "user", "content": "hi"}])


async def test_timeout_maps_to_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(LLMTimeout):
        await make_client(handler).chat([{"role": "user", "content": "hi"}])


async def test_auth_header_is_sent() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}},
        )

    await make_client(handler).chat([{"role": "user", "content": "hi"}])
    assert seen["auth"] == "Bearer sk-test"
