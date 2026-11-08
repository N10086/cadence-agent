"""L2 降级链的边界测试：L1 / L2 / L3 三层各自可见、可断言。

三条铁律对应三组断言：

1. **L1 换策略**：reason 超时 → 换 fast client 重做本轮；prompt 换成 fast 版，
   但工具集/温度/消息历史都不变（"只换 client"）。
2. **L2 单步**：fast 也失败 → 不再规划，最多 1 次工具调用 + 1 次措辞。
3. **L3 兜底**：Core 不可用 → `UPSTREAM_UNAVAILABLE`，保留已完成 actions，不假装成功。

每一层都必须留下两类证据：trace 里的 `kind=DEGRADE` + SSE 的 `degraded`。
只测"最终文本"是不够的 —— 静默降级也会给出一样的文本。
"""

from __future__ import annotations

from cadence.llm.base import LLMUnavailable
from cadence.llm.fake import FakeLLM, call, fail, say
from cadence.runtime.errors import ErrorCode, ToolError
from cadence.runtime.trace import KIND_DEGRADE, load_trace

from conftest import make_ctx, run_script


async def _run_with_primary(
    runtime,
    primary,
    fast: FakeLLM,
    message: str,
    *,
    tier: str | None = None,
):
    """`run_script` 的变体：允许主 client 不是 FakeLLM 本身（这里要包一层 FailOnce）。"""
    runtime.loop.llm = primary
    runtime.loop.fast_llm = fast
    ctx = make_ctx(runtime)
    session = runtime.sessions.get("s_test")
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    outcome = await runtime.loop.run_turn(
        ctx, user_message=message, session=session, emit=emit, tier=tier
    )
    ctx.trace.close(cost_yuan=outcome.cost_yuan, total_ms=0, degraded=outcome.degraded)

    class _Result:
        pass

    result = _Result()
    result.outcome = outcome
    result.events = events
    result.ctx = ctx
    return result

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


class FailOnceClient:
    """第一次抛错、之后转发的 client 包装。

    专门用来验证「换 client」而不是「换 tier 变量」：Loop 必须把**另一个对象**
    传进下一次调用，否则"只换 client"这条契约无法与"改个字符串"区分开。
    """

    name = "fail-once"

    def __init__(self, inner: FakeLLM) -> None:
        self.inner = inner
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None, **kw):
        return await self.inner.chat(messages, tools, **kw)

    async def stream(self, messages, tools=None, **kw):
        self.calls.append({"messages": list(messages), "tools": list(tools or [])})
        if len(self.calls) == 1:
            raise LLMUnavailable("provider 503")
        async for ev in self.inner.stream(messages, tools, **kw):
            yield ev


def _trace_kinds(runtime) -> list[tuple[str, str]]:
    data = load_trace(runtime.settings.resolved_trace_dir, "tr_test") or {}
    return [(s["kind"], s["name"]) for s in data.get("steps", [])]


def _has_degrade_event(events) -> bool:
    return any(e == "error" and d.get("degraded") for e, d in events)


# ---------------------------------------------------------------------------
# L1：换 fast client 重做本轮
# ---------------------------------------------------------------------------


async def test_l1_degrades_to_fast_client_and_is_visible(runtime) -> None:
    runtime.settings.llm_retry_times = 0
    primary = FailOnceClient(runtime.llm)
    fast = FakeLLM([say("快速模式下的回答")])

    result = await _run_with_primary(runtime, primary, fast, "帮我准备面试", tier="reason")

    assert len(primary.calls) == 1, "reason 只尝试一次就降级，不做同策略重试"
    assert fast.calls, "fast client 必须真的被调用"
    assert result.outcome.ok
    assert result.outcome.text == "快速模式下的回答"
    assert result.outcome.degraded is True
    assert result.outcome.degrade_reason.startswith("reason_model_unavailable")
    assert ("DEGRADE", "llm.degrade_l1") in _trace_kinds(runtime)
    assert _has_degrade_event(result.events)


async def test_l1_keeps_tools_and_history_only_swaps_client(runtime) -> None:
    """换 client 时参数不变：工具集仍是 10 个、消息历史仍是同一份。"""
    runtime.settings.llm_retry_times = 0
    primary = FailOnceClient(runtime.llm)
    fast = FakeLLM([say("好")])

    await _run_with_primary(runtime, primary, fast, "帮我准备面试", tier="reason")

    assert len(fast.calls) == 1
    sent = fast.calls[0]
    # 降级不改变工具集：L1 收到的 tools 必须和 L0（primary）完全一致。
    # 注意分类路由后工具集每轮可能小于 10，所以不能断言 ==10。
    assert sent["tools"] == primary.calls[0]["tools"], "降级不许削减或改变工具集"
    assert sent["messages"][0]["role"] == "system"
    assert sent["messages"][-1]["role"] == "user"


# ---------------------------------------------------------------------------
# L2：fast 也失败 → 单步模式
# ---------------------------------------------------------------------------


async def test_l2_falls_back_to_single_step(runtime) -> None:
    runtime.settings.llm_retry_times = 0
    # 第 1 次 reason 失败、第 2 次 fast 失败、第 3 次单步成功
    result = await run_script(
        runtime,
        [fail("timeout"), fail("timeout"), say("单步回答")],
        "帮我准备面试",
        tier="reason",
    )

    assert result.outcome.ok
    assert result.outcome.text == "单步回答"
    assert result.outcome.degraded is True
    assert result.outcome.degrade_reason.startswith("single_step_fallback")
    kinds = _trace_kinds(runtime)
    assert ("DEGRADE", "llm.degrade_l1") in kinds
    assert ("DEGRADE", "llm.degrade_l1_failed") in kinds
    assert ("DEGRADE", "llm.degrade_l2") in kinds
    assert _has_degrade_event(result.events)


async def test_l2_single_step_executes_exactly_one_tool(runtime, core) -> None:
    """单步模式只执行 1 次工具，且不再回到"规划→执行→再规划"的循环。"""
    runtime.settings.llm_retry_times = 0
    result = await run_script(
        runtime,
        [
            fail("timeout"),
            fail("timeout"),
            {
                "tool_calls": [
                    {"name": "task.search", "arguments": {"limit": 5}},
                    {"name": "task.search", "arguments": {"keyword": "牛奶"}},
                ]
            },
            say("单步收尾"),
        ],
        "帮我准备面试",
        tier="reason",
    )

    assert result.outcome.degraded is True
    searches = [c for c in core.calls if c["path"] == "/api/v1/tasks"]
    assert len(searches) == 1, "单步只允许一次工具调用"
    assert ("GUARD", "guard.single_step") in _trace_kinds(runtime)


async def test_l2_single_step_core_down_is_l3(runtime, core) -> None:
    """单步里执行工具时 Core 挂了 → L3：UPSTREAM_UNAVAILABLE，保留已完成进度。"""
    runtime.settings.llm_retry_times = 0

    async def flaky_request(method, path, **kw):
        if method == "PATCH":
            raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "Core 连接失败", core_down=True)
        # 搜索成功一次，把 t_001 放进白名单/可见版本
        return {"items": [{"id": "t_001", "version": 0, "title": "买牛奶"}], "total": 1}

    core.request = flaky_request  # type: ignore[assignment]

    # 第 1 次 reason 超时 → L1 fast 成功拿到搜索结果；
    # 第 2 轮 fast 超时 → L2 单步：此时 Core 的 PATCH 挂掉 → L3
    result = await run_script(
        runtime,
        [
            fail("timeout"),
            call("task.search", limit=5),
            fail("timeout"),
            call("task.update", task_id="t_001", version=0, patch={"priority": "HIGH"}),
        ],
        "帮我准备面试",
        tier="reason",
    )

    assert result.outcome.degraded is True
    assert result.outcome.error is not None
    assert result.outcome.error.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert result.outcome.error.core_down is True
    assert "已经完成的" in result.outcome.text
    assert "任务服务" in result.outcome.text


# ---------------------------------------------------------------------------
# L3：Core 不可用（正常多步路径）
# ---------------------------------------------------------------------------


async def test_l3_core_down_after_successful_tool_aborts_fast(runtime, core) -> None:
    """模型正常、Core 挂：不该再白跑 LLM 步，直接 UPSTREAM_UNAVAILABLE。"""
    runtime.settings.llm_retry_times = 0

    async def broken_request(*args, **kw):
        raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "Core 连接失败", core_down=True)

    core.request = broken_request  # type: ignore[assignment]

    result = await run_script(
        runtime,
        [call("task.search", limit=5), say("不该走到这里")],
        "看看任务",
    )

    assert result.outcome.error is not None
    assert result.outcome.error.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert "已经完成的" in result.outcome.text
    assert _has_degrade_event(result.events) is False  # 没降级，只是上游挂了


async def test_business_error_does_not_abort_the_turn(runtime) -> None:
    """409/422/幻觉 id 是业务错误，必须喂回模型自纠，而不是当成 L3。"""
    result = await run_script(
        runtime,
        [call("task.update", task_id="t_999", version=1, patch={"priority": "HIGH"}), say("没找到，换一个")],
        "改一下",
    )
    assert result.outcome.ok
    assert result.outcome.text == "没找到，换一个"


# ---------------------------------------------------------------------------
# 熔断 OPEN 期间不打 provider
# ---------------------------------------------------------------------------


async def test_breaker_open_never_calls_provider(runtime) -> None:
    runtime.settings.llm_retry_times = 0
    breaker = runtime.breaker
    for _ in range(4):
        breaker.record(False)
    assert breaker.state == "OPEN"
    assert breaker.allow() is False

    provider_before = len(runtime.llm.calls)
    result = await run_script(runtime, [say("不该被调用")], "看看任务")

    assert len(runtime.llm.calls) == provider_before, "OPEN 期间一次 provider 调用都不许发生"
    assert result.outcome.error is not None
    assert result.outcome.error.code is ErrorCode.UPSTREAM_UNAVAILABLE


# ---------------------------------------------------------------------------
# 降级必须落 trace（kind=DEGRADE）+ detail 带 reason
# ---------------------------------------------------------------------------


async def test_degrade_is_written_as_its_own_trace_kind(runtime) -> None:
    runtime.settings.llm_retry_times = 0
    await run_script(
        runtime,
        [fail("timeout"), fail("timeout"), say("单步回答")],
        "帮我准备面试",
        tier="reason",
    )
    data = load_trace(runtime.settings.resolved_trace_dir, "tr_test") or {}
    degrade_steps = [s for s in data["steps"] if s["kind"] == KIND_DEGRADE]
    assert degrade_steps, "降级必须有独立 kind"
    assert any(s["detail"].get("error") or s["detail"].get("from") for s in degrade_steps), \
        "DEGRADE 的 detail 必须带 reason"


# ---------------------------------------------------------------------------
# fast 轮次失败：没有 L1 可换，直接 L2
# ---------------------------------------------------------------------------


async def test_fast_turn_goes_straight_to_single_step(runtime) -> None:
    runtime.settings.llm_retry_times = 0
    result = await run_script(
        runtime,
        [fail("timeout"), say("单步回答")],
        "看看任务",  # 纯查询 → fast tier
        tier="fast",
    )

    assert result.outcome.ok
    assert result.outcome.degraded is True
    kinds = _trace_kinds(runtime)
    assert ("DEGRADE", "llm.degrade_l1") not in kinds, "fast 没有更低一档可换，不该假装走了 L1"
    assert ("DEGRADE", "llm.degrade_l2") in kinds
    assert result.outcome.text == "单步回答"
