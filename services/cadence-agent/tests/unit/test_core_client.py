"""CoreClient：外壳拆解 + 状态码翻译 + 重试。

这一层是唯一跟 Core 的 HTTP 打交道的地方，所以 HTTP 语义全在这里测。
"""

from __future__ import annotations

import httpx
import pytest

from cadence.config import Settings
from cadence.runtime.errors import ErrorCode, ToolError
from cadence.tools.core_client import CoreClient, classify_422


def make_settings(**kw) -> Settings:
    base = dict(llm_mode="fake", deepseek_api_key="", core_retry_times=2, core_retry_backoff_s=0.0)
    base.update(kw)
    return Settings(**base)


async def no_sleep(_s: float) -> None:
    return None


def client_with(handler, settings: Settings | None = None) -> CoreClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="http://core.test", transport=transport)
    return CoreClient(settings or make_settings(), http=http, sleep=no_sleep)


def json_response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body)


# ---------------------------------------------------------------------------


async def test_success_unwraps_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, {"code": 0, "message": "success", "data": {"items": [], "total": 0}})

    data = await client_with(handler).request("GET", "/api/v1/tasks")
    assert data == {"items": [], "total": 0}


async def test_204_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    assert await client_with(handler).request("DELETE", "/api/v1/tasks/t_1") is None


async def test_403_carries_confirm_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            403,
            {"code": 403, "message": "需要确认", "data": {"confirmToken": "ct_x", "snapshot": {"id": "t_1"}}},
        )

    with pytest.raises(ToolError) as exc:
        await client_with(handler).request("DELETE", "/api/v1/tasks/t_1")
    assert exc.value.code is ErrorCode.CONFIRM_REQUIRED
    assert (exc.value.details or {})["confirmToken"] == "ct_x"


async def test_404_returns_search_suggestion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(404, {"code": 404, "message": "任务不存在"})

    with pytest.raises(ToolError) as exc:
        await client_with(handler).request("GET", "/api/v1/tasks/t_999")
    assert exc.value.code is ErrorCode.TASK_ID_NOT_FOUND
    assert exc.value.suggestions


async def test_409_is_conflict_and_recoverable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(409, {"code": 409, "message": "版本冲突"})

    with pytest.raises(ToolError) as exc:
        await client_with(handler).request("PATCH", "/api/v1/tasks/t_1", body={"version": 1})
    assert exc.value.code is ErrorCode.CONFLICT
    assert exc.value.recoverable is True


async def test_422_dependency_vs_illegal_transition() -> None:
    dep = classify_422("前置依赖未完成，不能完成该任务", "task.complete")
    illegal = classify_422("已取消的任务不能标记完成", "task.update")
    assert dep is ErrorCode.DEPENDENCY_VIOLATION
    assert illegal is ErrorCode.ILLEGAL_TRANSITION


async def test_422_complete_defaults_to_dependency() -> None:
    """Core 的 422 不带符号码，只能靠工具语义兜底：complete 的 422 多半是前置没做完。"""
    assert classify_422("业务规则不允许", "task.complete") is ErrorCode.DEPENDENCY_VIOLATION
    assert classify_422("业务规则不允许", "task.update") is ErrorCode.ILLEGAL_TRANSITION


async def test_5xx_retries_then_raises() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return json_response(500, {"code": 500, "message": "boom"})

    client = client_with(handler)
    with pytest.raises(ToolError) as exc:
        await client.request("GET", "/api/v1/tasks")
    assert exc.value.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert attempts["n"] == 3, "首次 + 2 次重试"
    assert client.call_count == 3


async def test_5xx_then_success_is_transparent() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return json_response(500, {"code": 500, "message": "boom"})
        return json_response(200, {"code": 0, "message": "success", "data": {"ok": True}})

    data = await client_with(handler).request("GET", "/api/v1/tasks")
    assert data == {"ok": True}
    assert attempts["n"] == 2


async def test_4xx_is_not_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return json_response(404, {"code": 404, "message": "没有"})

    with pytest.raises(ToolError):
        await client_with(handler).request("GET", "/api/v1/tasks/t_x")
    assert attempts["n"] == 1


async def test_non_json_response_is_structured_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"<html>bad gateway</html>")

    with pytest.raises(ToolError) as exc:
        await client_with(handler).request("GET", "/api/v1/tasks")
    assert exc.value.code is ErrorCode.UPSTREAM_UNAVAILABLE


async def test_headers_are_forwarded() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return json_response(200, {"code": 0, "message": "success", "data": None})

    await client_with(handler).request(
        "POST",
        "/api/v1/tasks",
        body={"title": "x"},
        trace_id="tr_1",
        idempotency_key="idem_1",
        actor="agent",
    )
    assert seen["x-actor"] == "agent"
    assert seen["x-trace-id"] == "tr_1"
    assert seen["x-idempotency-key"] == "idem_1"
