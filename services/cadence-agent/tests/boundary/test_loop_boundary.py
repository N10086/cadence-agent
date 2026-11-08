"""循环状态机的边界测试。

覆盖：步数预算、指纹去重、澄清短路、确认流程、多工具调用的顺序、
降级/重试/非法输出、白名单拦截、上下文压缩触发。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from cadence.llm.fake import bad_json, call, calls, fail, say, tc
from cadence.runtime.budget import Budget
from cadence.runtime.context import TurnContext
from cadence.runtime.errors import ErrorCode
from cadence.runtime.loop import TurnOutcome
from cadence.runtime.trace import TraceRecorder, load_trace


@dataclass
class _Run:
    outcome: TurnOutcome
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def of(self, event: str) -> list[dict[str, Any]]:
        return [d for e, d in self.events if e == event]

    @property
    def text_tokens(self) -> str:
        return "".join(d.get("text", "") for d in self.of("token"))


def _ctx(rt, *, session_id: str = "s_test", trace_id: str = "tr_test") -> TurnContext:
    trace = TraceRecorder(trace_id, rt.settings.resolved_trace_dir, session_id=session_id)
    return TurnContext(
        settings=rt.settings,
        core=rt.core,
        trace=trace,
        confirms=rt.confirms,
        trace_id=trace_id,
        session_id=session_id,
        budget=Budget(max_steps=rt.settings.step_budget, max_tokens=rt.settings.token_budget_per_turn),
    )


async def _run(rt, script, message, *, tier=None, session_id="s_test", ctx=None) -> _Run:
    rt.llm.load(script)
    ctx = ctx or _ctx(rt, session_id=session_id)
    session = rt.sessions.get(session_id)
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, data: dict[str, Any]) -> None:
        events.append((event, data))

    outcome = await rt.loop.run_turn(ctx, user_message=message, session=session, emit=emit, tier=tier)
    ctx.trace.close(cost_yuan=outcome.cost_yuan, total_ms=0, degraded=outcome.degraded)
    return _Run(outcome=outcome, events=events)


# =====================================================================
# 预算与中断
# =====================================================================


async def test_step_budget_stops_at_max_and_reports_progress(runtime, core) -> None:
    ctx = _ctx(runtime, session_id="s_step")
    ctx.budget = Budget(max_steps=2, max_tokens=200_000)
    out = await _run(
        runtime,
        [call("task.search", keyword="a"), call("task.search", keyword="b"), call("task.search", keyword="c")],
        "查一下",
        ctx=ctx,
    )
    assert out.outcome.error is not None
    assert out.outcome.error.code is ErrorCode.BUDGET_EXCEEDED
    assert out.outcome.steps == 2
    assert "步数预算用尽" in out.outcome.text


async def test_fingerprint_duplicate_call_aborts(runtime, core) -> None:
    out = await _run(
        runtime,
        [call("task.search", keyword="same"), call("task.search", keyword="same")],
        "查一下",
    )
    assert out.outcome.error is not None
    assert out.outcome.error.code is ErrorCode.LOOP_DETECTED
    assert "重复调用" in out.outcome.text
    # 第一次 search 真的执行了，第二次在本地被拦
    assert len([c for c in core.calls if c["path"] == "/api/v1/tasks"]) == 1


async def test_token_budget_aborts(runtime) -> None:
    ctx = _ctx(runtime, session_id="s_tokens")
    ctx.budget = Budget(max_steps=8, max_tokens=100)
    out = await _run(runtime, [say("x" * 10, tokens_out=1000)], "hi", ctx=ctx)
    assert out.outcome.error is not None
    assert out.outcome.error.code is ErrorCode.BUDGET_EXCEEDED
    assert "token 预算用尽" in out.outcome.text


# =====================================================================
# 澄清
# =====================================================================


async def test_ambiguous_message_short_circuits_before_llm(runtime, llm) -> None:
    out = await _run(runtime, [say("不应该走到这里")], "把那个搞一下")
    assert out.outcome.clarify is not None
    assert out.outcome.clarify["question"]
    assert out.of("clarify")
    assert llm.calls == [], "歧义必须先问，不应先花钱调模型"


async def test_clarify_is_emitted_before_any_tool_call(runtime, core) -> None:
    out = await _run(runtime, [call("task.search")], "把那个报告搞一下")
    assert out.outcome.clarify is not None
    assert core.calls == []


async def test_empty_message_is_clarified(runtime, llm) -> None:
    out = await _run(runtime, [say("x")], "   ")
    assert out.outcome.clarify is not None
    assert llm.calls == []


# =====================================================================
# 确认
# =====================================================================


async def test_delete_requires_confirm_and_does_not_delete_yet(runtime, core) -> None:
    out = await _run(
        runtime,
        [call("task.search", limit=10), call("task.delete", task_id="t_001")],
        "把买牛奶删掉",
    )
    assert out.outcome.confirm is not None
    assert out.outcome.confirm["confirmToken"].startswith("cf_")
    assert out.outcome.confirm["affected"] == 1
    assert "t_001" in core.tasks
    assert out.of("confirm")


async def test_confirm_token_can_execute_a_different_task_than_previewed(runtime, core) -> None:
    """BUG-SP2-05：cf_ 令牌只绑定 tool 名，不绑定参数/版本。"""
    token = runtime.confirms.issue(
        tool="task.delete",
        args={"task_id": "t_001"},
        preview={"kind": "delete"},
        versions={"t_001": 0},
    ).token
    out = await _run(
        runtime,
        [call("task.search", limit=10), call("task.delete", task_id="t_002", confirm_token=token)],
        "确认删除",
    )
    # 当前实现会消费令牌并删除 t_002，尽管用户确认的是 t_001
    assert "t_002" not in core.tasks
    assert "t_001" in core.tasks


@pytest.mark.xfail(strict=True, reason="BUG-SP2-05：确认令牌必须绑定 args（至少 task_id），"
                                       "否则确认 A 可以删 B")
async def test_bug_confirm_token_scope_is_bound_to_args(runtime, core) -> None:
    token = runtime.confirms.issue(tool="task.delete", args={"task_id": "t_001"}).token
    await _run(
        runtime,
        [call("task.search", limit=10), call("task.delete", task_id="t_002", confirm_token=token)],
        "确认删除",
    )
    assert "t_002" in core.tasks


async def test_invalid_confirm_token_issues_new_confirm(runtime, core) -> None:
    out = await _run(
        runtime,
        [call("task.search", limit=10), call("task.delete", task_id="t_001", confirm_token="cf_nope")],
        "删除",
    )
    assert out.outcome.confirm is not None
    assert out.outcome.confirm["confirmToken"] != "cf_nope"
    assert "t_001" in core.tasks


# =====================================================================
# 多工具调用与 finish
# =====================================================================


async def test_multiple_tool_calls_in_one_response_all_execute(runtime, core) -> None:
    out = await _run(
        runtime,
        [calls(tc("task.search", limit=10), tc("task.complete", task_id="t_002", version=1)), say("完成")],
        "完成周报",
    )
    assert core.tasks["t_002"]["status"] == "DONE"
    assert out.outcome.actions_taken


async def test_meta_finish_first_drops_later_calls(runtime, core) -> None:
    """BUG-SP2-13：同一批 tool_call 中 finish 排在前，后面的调用会被静默丢弃。"""
    out = await _run(
        runtime,
        [calls(
            tc("task.create", title="新任务"),
            tc("meta.finish", message="好了", actions_taken=["创建任务「新任务」"]),
            tc("task.complete", task_id="t_001", version=1),
        )],
        "先建再完成",
    )
    assert out.outcome.text == "好了"
    assert not any(c["path"].endswith(":complete") for c in core.calls)


@pytest.mark.xfail(strict=True, reason="BUG-SP2-13：finish 后的 tool_call 必须被处理或明确拒绝，"
                                       "不能静默丢弃（模型会以为都执行了）")
async def test_bug_finish_does_not_drop_later_calls(runtime, core) -> None:
    await _run(
        runtime,
        [calls(
            tc("task.create", title="新任务"),
            tc("meta.finish", message="好了", actions_taken=[]),
            tc("task.complete", task_id="t_001", version=1),
        )],
        "先建再完成",
    )
    assert any(c["path"].endswith(":complete") for c in core.calls)


async def test_finish_flags_unverified_claim(runtime, core) -> None:
    out = await _run(
        runtime,
        [call("task.create", title="x"),
         call("meta.finish", message="完成", actions_taken=["删除了任务 A"])],
        "做点事",
    )
    assert "有些动作我其实没有执行成功" in out.outcome.text


async def test_finish_read_hint_marks_claim_verified_current_behavior(runtime, core) -> None:
    """BUG-SP2-12（meta.finish 的宽松匹配）：一句"我检查了一下并删了 3 个"会被当成真话。"""
    out = await _run(
        runtime,
        [call("task.create", title="x"),
         call("meta.finish", message="完成", actions_taken=["我检查了一下并删除了 3 个任务"])],
        "做点事",
    )
    assert "有些动作我其实没有执行成功" not in out.outcome.text


@pytest.mark.xfail(strict=True, reason="BUG-SP2-12：actions_taken 的校验被 READ_HINTS 放水，"
                                       "包含'检查/查看'的虚假动作能通过")
async def test_bug_finish_should_flag_read_hint_with_false_action(runtime, core) -> None:
    out = await _run(
        runtime,
        [call("task.create", title="x"),
         call("meta.finish", message="完成", actions_taken=["我检查了一下并删除了 3 个任务"])],
        "做点事",
    )
    assert "有些动作我其实没有执行成功" in out.outcome.text


# =====================================================================
# 护栏与失败矩阵
# =====================================================================


async def test_unknown_tool_is_reported_and_loop_continues(runtime) -> None:
    out = await _run(runtime, [call("sql.execute", q="drop table"), say("好的")], "删库")
    results = out.of("tool_result")
    assert results and results[0]["ok"] is False
    assert results[0]["code"] == "VALIDATION_ERROR"
    assert out.outcome.text == "好的"


async def test_hallucinated_id_never_reaches_core(runtime, core) -> None:
    out = await _run(
        runtime,
        [call("task.update", task_id="t_999", version=1, patch={"title": "x"}), say("没找到")],
        "改一下",
    )
    assert not any(c["method"] == "PATCH" for c in core.calls)
    results = out.of("tool_result")
    assert results and results[0]["code"] == "TASK_ID_NOT_IN_CONTEXT"


async def test_llm_timeout_degrades_to_fast(runtime) -> None:
    runtime.settings.llm_retry_times = 1
    out = await _run(
        runtime,
        [fail("timeout"), fail("timeout"), say("降级回答")],
        "帮我准备面试",
        tier="reason",
    )
    assert out.outcome.degraded is True
    assert out.outcome.degrade_reason.startswith("reason_model_unavailable")
    assert "降级回答" in out.outcome.text
    assert any(e == "error" and d.get("code") == "DEGRADED" for e, d in out.events)


async def test_llm_bad_output_falls_back_to_human_text(runtime) -> None:
    runtime.settings.llm_retry_times = 1
    out = await _run(runtime, [bad_json(), bad_json()], "看看任务")
    assert "没太理解" in out.outcome.text
    assert out.outcome.error is None


async def test_no_tool_call_is_plain_answer(runtime) -> None:
    out = await _run(runtime, [say("你好，有什么可以帮你？")], "你好")
    assert out.outcome.text.startswith("你好")
    assert out.text_tokens == "你好，有什么可以帮你？"


# =====================================================================
# 上下文压缩
# =====================================================================


async def test_context_compression_is_traced_when_over_budget(runtime) -> None:
    runtime.settings.max_context_tokens = 1
    await _run(runtime, [say("x")], "hi")
    names = [s.name for s in _trace_steps(runtime)]
    assert "guard.compress" in names


async def test_session_summary_is_never_generated(runtime) -> None:
    """BUG-SP2-07：session.summary 永远是空串；长对话只保留最近 3 轮。"""
    session = runtime.sessions.get("s_summary")
    await _run(runtime, [say("hi")], "hi", session_id="s_summary")
    session.add_turn("user", "第一轮")
    assert session.summary == ""


def _trace_steps(runtime) -> list:
    # 当前测试里 trace 文件按 trace_id 落盘；直接读回
    from cadence.runtime.trace import load_trace

    data = load_trace(runtime.settings.resolved_trace_dir, "tr_test")
    return [type("S", (), {"name": s["name"]}) for s in (data or {}).get("steps", [])]
