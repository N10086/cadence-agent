"""Trace：可回放、可归因。"""

from __future__ import annotations

from cadence.llm.fake import call, say
from cadence.runtime.trace import KIND_DEGRADE, format_timeline, load_trace

from conftest import run_script


async def test_trace_file_is_written(runtime) -> None:
    result = await run_script(runtime, [call("task.search", limit=5), say("好。")], "看看任务")
    path = result.ctx.trace.path
    assert path.exists()
    assert path.name.endswith(".jsonl")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 3, "至少 META + 若干步骤 + SUMMARY"
    assert '"kind": "META"' in lines[0]


async def test_trace_can_be_reloaded(runtime) -> None:
    await run_script(runtime, [call("task.search", limit=5), say("好。")], "看看任务")
    trace = load_trace(runtime.settings.resolved_trace_dir, "tr_test")
    assert trace is not None
    assert trace["traceId"] == "tr_test"
    assert trace["steps"]
    assert trace["promptVersion"], "prompt 版本必须记下来，否则改 prompt 无法归因"


async def test_trace_records_tool_and_llm_kinds(runtime) -> None:
    await run_script(runtime, [call("task.search", limit=5), say("好。")], "看看任务")
    trace = load_trace(runtime.settings.resolved_trace_dir, "tr_test")
    kinds = {s["kind"] for s in trace["steps"]}
    assert {"LLM", "TOOL"} <= kinds


async def test_trace_uses_camel_case_on_the_wire(runtime) -> None:
    """字段名是对外契约的一部分：前端按 durationMs 取值，给 snake_case 就是 undefined。"""
    await run_script(runtime, [call("task.search", limit=5), say("好。")], "看看任务")
    trace = load_trace(runtime.settings.resolved_trace_dir, "tr_test")
    assert trace is not None
    for key in ("traceId", "totalMs", "costYuan", "steps"):
        assert key in trace
    step = trace["steps"][0]
    for key in ("seq", "kind", "name", "durationMs", "tokensIn", "tokensOut", "ok", "detail"):
        assert key in step, f"step 缺字段 {key}"
    assert "duration_ms" not in step, "不该把库内 snake_case 直接暴露出去"


async def test_trace_records_guard_rejection(runtime, core) -> None:
    from cadence.llm.fake import call as c

    await run_script(
        runtime,
        [c("task.update", task_id="t_999", version=1, patch={"title": "x"}), say("好。")],
        "改一下报告",
    )
    trace = load_trace(runtime.settings.resolved_trace_dir, "tr_test")
    whitelist = [s for s in trace["steps"] if s["name"] == "guard.whitelist"]
    assert whitelist and whitelist[0]["ok"] is False
    assert whitelist[0]["detail"]["offending"] == "t_999"


async def test_degrade_is_recorded_as_its_own_kind(runtime) -> None:
    """DEGRADE 是新增的一类：没有它，SP4 算不出 degraded_rate。"""
    from cadence.llm.base import LLMTimeout
    from cadence.llm.fake import fail

    await run_script(
        runtime,
        [fail(LLMTimeout("x")), fail(LLMTimeout("x")), say("降级回答")],
        "帮我准备面试",
        tier="reason",
    )
    trace = load_trace(runtime.settings.resolved_trace_dir, "tr_test")
    assert any(s["kind"] == KIND_DEGRADE for s in trace["steps"])


async def test_format_timeline_is_human_readable(runtime) -> None:
    await run_script(runtime, [call("task.search", limit=5), say("好。")], "看看任务")
    trace = load_trace(runtime.settings.resolved_trace_dir, "tr_test")
    assert trace is not None
    text = format_timeline(trace)
    assert "trace tr_test" in text
    assert "task.search" in text
    assert "ms" in text
