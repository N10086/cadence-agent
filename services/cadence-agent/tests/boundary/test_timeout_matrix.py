"""L2-3 超时矩阵：LLM connect 3s / read 15s；Core connect 2s / read 3s；单轮 60s。

为什么这些必须测，而且必须测**接线**而不只是测常量：

- 常量写对但没传进 client，等于没配（原来 DeepSeek 客户端把 20s 写死在构造里）；
- 确认流程里 `_assert_confirm_versions` 的那次 GET 是 **Core 读**，
  如果误用 LLM 的 15s，Core 挂掉时确认会白等 15 秒，并且错误会被归因到模型侧。
  这条以前真的踩过，所以用"记录的 timeout 值 + 不调用 LLM"两条一起钉住。
"""

from __future__ import annotations

import time

import httpx
import pytest

from cadence.config import Settings
from cadence.llm.base import LLMTimeout
from cadence.llm.deepseek import DeepSeekClient
from cadence.runtime.errors import ErrorCode
from cadence.tools.core_client import CoreClient


def test_matrix_defaults_are_the_contract_numbers() -> None:
    s = Settings(llm_mode="fake", deepseek_api_key="")
    assert s.llm_connect_timeout_s == 3.0
    assert s.llm_read_timeout_s == 15.0
    assert s.core_connect_timeout_s == 2.0
    assert s.core_timeout_s == 3.0
    assert s.turn_timeout_s == 60.0


def test_llm_timeouts_helper_returns_matrix_and_allows_provider_override() -> None:
    s = Settings(llm_mode="fake", deepseek_api_key="")
    assert s.llm_timeouts("deepseek") == (3.0, 15.0)
    # 0 表示"用矩阵默认"，不是"没有超时"
    assert s.llm_timeouts("zhipu") == (3.0, 15.0)

    z = Settings(llm_mode="fake", deepseek_api_key="", zhipu_timeout_s=8.0)
    connect, read = z.llm_timeouts("zhipu")
    assert connect == 3.0 and read == 8.0, "provider 只能覆盖 read，connect 仍走矩阵"


async def test_core_client_sends_core_timeout_not_llm_timeout() -> None:
    seen: dict[str, float | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.extensions.get("timeout") or {})
        return httpx.Response(200, json={"code": 0, "message": "success", "data": {"ok": True}})

    settings = Settings(llm_mode="fake", deepseek_api_key="")
    http = httpx.AsyncClient(base_url="http://core.test", transport=httpx.MockTransport(handler))
    client = CoreClient(settings, http=http)

    await client.request("GET", "/api/v1/tasks")

    assert seen.get("connect") == 2.0
    assert seen.get("read") == 3.0
    assert seen.get("read") != 15.0, "Core 绝不能借用 LLM 的 read 超时"


async def test_core_read_timeout_is_reported_as_core_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow core", request=request)

    settings = Settings(llm_mode="fake", deepseek_api_key="", core_retry_times=0)
    http = httpx.AsyncClient(base_url="http://core.test", transport=httpx.MockTransport(handler))
    client = CoreClient(settings, http=http)

    with pytest.raises(Exception) as exc:
        await client.request("GET", "/api/v1/tasks")

    err = exc.value
    assert err.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert err.core_down is True, "传输层失败必须打 core_down，L3 才能识别"


async def test_deepseek_client_uses_matrix_timeouts() -> None:
    s = Settings(llm_mode="fake", deepseek_api_key="")
    connect, read = s.llm_timeouts("deepseek")
    client = DeepSeekClient(
        api_key="k", base_url="http://llm.test", model="m",
        timeout_s=read, connect_timeout_s=connect,
    )
    assert client.timeout_s == 15.0
    assert client.connect_timeout_s == 3.0


# ---------------------------------------------------------------------------
# 确认流程的那次 GET 走 Core 超时（不是 LLM 的 15s）
# ---------------------------------------------------------------------------


async def test_confirm_version_check_get_uses_core_timeout(runtime, core, settings) -> None:
    """`_assert_confirm_versions` 的 GET 必须带 Core 的 timeout（connect 2 / read 3）。

    做法：把 runtime.core 换成一个**真** CoreClient + MockTransport，
    这样能读到 httpx 请求上真实挂着的 timeout，而不是我们自己的配置对象。
    """
    from cadence.llm.fake import call
    from cadence.tools.core_client import CoreClient as _CoreClient

    from conftest import run_script

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"method": request.method, "path": request.url.path,
                     "timeout": dict(request.extensions.get("timeout") or {})})
        if request.method == "GET" and request.url.path.startswith("/api/v1/tasks/"):
            # 与 boundary conftest 的 t_001 版本一致（version=0），令牌校验才会通过
            return httpx.Response(200, json={
                "code": 0, "message": "success", "data": {"task": {"id": "t_001", "version": 0}},
            })
        return httpx.Response(200, json={
            "code": 0, "message": "success",
            "data": {"items": [{"id": "t_001", "title": "买牛奶", "version": 0}], "total": 1},
        })

    http = httpx.AsyncClient(base_url="http://core.test", transport=httpx.MockTransport(handler))
    # 直接换掉 runtime.core（不是改 .request，否则 routed 里的 fallback 会递归到自己）
    runtime.core = _CoreClient(settings, http=http)  # type: ignore[assignment]

    # 第一轮：搜索把 t_001 放进白名单并记下 version，delete 生成确认令牌
    first = await run_script(
        runtime,
        [call("task.search", limit=5), call("task.delete", task_id="t_001")],
        "把买牛奶删掉",
    )
    confirmer = first.of("confirm")
    assert confirmer, "删除必须先要确认"
    token = confirmer[0]["confirmToken"]

    # 第二轮：用真令牌重放 → 触发 _assert_confirm_versions 的 GET
    replay = await run_script(
        runtime,
        [call("task.delete", task_id="t_001", confirm_token=token)],
        "确认删除",
    )

    gets = [c for c in seen if c["path"] == "/api/v1/tasks/t_001"]
    assert gets, "确认流程必须重新读一次版本"
    assert gets[-1]["timeout"].get("connect") == 2.0
    assert gets[-1]["timeout"].get("read") == 3.0
    assert replay.outcome.error is None or replay.outcome.error.code is not ErrorCode.PLAN_STALE


# ---------------------------------------------------------------------------
# LLM 读超时 → LLMTimeout → 走降级链，而不是卡住整轮
# ---------------------------------------------------------------------------


class HungClient:
    """永远不返回 done 的流：模拟"provider 连上了但不说话"。"""

    name = "hung"

    async def chat(self, messages, tools=None, **kw):  # pragma: no cover - 不走
        raise AssertionError("不该调用 chat")

    async def stream(self, messages, tools=None, **kw):
        import asyncio

        await asyncio.sleep(10)
        if False:  # pragma: no cover - 让它是 async generator
            yield None


async def test_llm_read_timeout_turns_into_llm_timeout(settings) -> None:
    from cadence.config import Settings as S
    from cadence.runtime.budget import Budget
    from cadence.runtime.context import TurnContext
    from cadence.runtime.loop import Loop
    from cadence.runtime.trace import TraceRecorder
    from cadence.guard.confirm import ConfirmStore
    from cadence.tools.registry import build_default_registry

    settings = S(
        llm_mode="fake", deepseek_api_key="",
        llm_read_timeout_s=0.05, llm_retry_times=0,
        data_dir=str(settings.data_dir) + "_timeout",
    )
    settings.ensure_dirs()
    registry = build_default_registry()
    loop = Loop(settings=settings, registry=registry, llm=HungClient())

    ctx = TurnContext(
        settings=settings,
        core=object(),  # type: ignore[arg-type]
        trace=TraceRecorder("tr_to", settings.resolved_trace_dir),
        confirms=ConfirmStore(10),
        trace_id="tr_to",
        session_id="s_to",
        budget=Budget(),
    )

    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    started = time.perf_counter()
    with pytest.raises(LLMTimeout):
        await loop._call_llm(HungClient(), [{"role": "user", "content": "hi"}], ctx, emit, "fast")
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"读超时没生效，等了 {elapsed:.2f}s"
