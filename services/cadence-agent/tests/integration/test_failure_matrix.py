"""★ 9 类失败场景，各一条测试。

这张表就是 PRD 02 §7.1 的失败矩阵，也是交付检查单的第 1 条：
"9 类失败场景各有 1 条自动化测试，全绿"。

| 编号 | 失败 | 注入方式 |
|---|---|---|
| F1 | 模型输出非法 JSON | FakeLLM 吐坏 JSON |
| F2 | 工具参数缺失 | 调 update 不给 version |
| F3 | 工具 404（id 不存在） | search 可见 → 期间被删 |
| F4 | 工具 5xx | FakeCore 抛 UPSTREAM_UNAVAILABLE |
| F5 | 循环打转 | 连续两次同样的调用 |
| F6 | 幻觉 id | 用一个没 search 过的 id |
| F7 | 危险操作 | 删任务不带确认 |
| F8 | 上下文超限 | 把上下文预算压到很小 |
| F9 | 模型超时 | FakeLLM 抛 LLMTimeout |
"""

from __future__ import annotations

from cadence.llm.base import LLMBadOutput, LLMTimeout
from cadence.llm.fake import bad_json, call, fail, say
from cadence.runtime.errors import ErrorCode

from conftest import run_script


# ---------------------------------------------------------------------------
# F1 模型输出非法 JSON
# ---------------------------------------------------------------------------


async def test_f1_bad_json_falls_back_to_plain_text(runtime) -> None:
    """重试 1 次仍非法 → 降级为一句人话，而不是抛错误。"""
    result = await run_script(runtime, [bad_json(), bad_json()], "把买牛奶标记完成")

    assert result.outcome.ok, "不该把内部解析错误抛给用户"
    assert "没太理解" in result.outcome.text
    assert result.outcome.error is None


# ---------------------------------------------------------------------------
# F2 工具参数缺失
# ---------------------------------------------------------------------------


async def test_f2_missing_args_becomes_clarification(runtime) -> None:
    """参数缺失不当作错误，而是当作歧义 → 问用户。"""
    result = await run_script(runtime, [call("task.update", task_id="t_001")], "把买牛奶标记完成")

    assert result.outcome.ok
    assert result.outcome.clarify is not None
    assert "version" in result.outcome.clarify["question"]
    assert result.of("clarify"), "必须推一个 clarify 事件给前端"


# ---------------------------------------------------------------------------
# F3 工具 404
# ---------------------------------------------------------------------------


async def test_f3_404_when_task_deleted_mid_flight(runtime, core) -> None:
    """这是 TASK_ID_NOT_FOUND 唯一能真实发生的场景：

    search 时还在（所以进了白名单），执行时已经被别人删了。
    如果白名单先拦住，404 永远不会发生 —— 所以这条测试必须这么构造。
    """
    removed = {"done": False}

    def on_request(method: str, path: str) -> None:
        if path.endswith(":complete") and not removed["done"]:
            core.remove_externally("t_001")
            removed["done"] = True

    core.on_request = on_request

    script = [
        call("task.search", limit=5),
        call("task.complete", task_id="t_001", version=2),
        say("这个任务好像已经被删掉了。"),
    ]
    result = await run_script(runtime, script, "把买牛奶标记完成")

    assert result.outcome.ok
    failed = [d for d in result.of("tool_result") if d["ok"] is False]
    assert failed, "工具应该失败"
    assert "不存在" in failed[0]["summary"]


# ---------------------------------------------------------------------------
# F4 工具 5xx
# ---------------------------------------------------------------------------


async def test_f4_upstream_5xx_is_fed_back_to_model(runtime, core) -> None:
    from cadence.runtime.errors import ToolError

    core.next_error = ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "模拟 Core 5xx")
    core.raise_times = 1

    script = [call("task.search", limit=5), say("任务服务暂时不可用，稍后再试。")]
    result = await run_script(runtime, script, "看看我的任务")

    assert result.outcome.ok
    failed = [d for d in result.of("tool_result") if d["ok"] is False]
    assert failed and "5xx" in failed[0]["summary"]


# ---------------------------------------------------------------------------
# F5 循环打转
# ---------------------------------------------------------------------------


async def test_f5_repeated_call_aborts_with_progress(runtime, core) -> None:
    script = [
        call("task.search", keyword="牛奶"),
        call("task.search", keyword="牛奶"),  # 完全一样 → 卡死
    ]
    result = await run_script(runtime, script, "找一下牛奶")

    assert result.outcome.error is not None
    assert result.outcome.error.code is ErrorCode.LOOP_DETECTED
    assert "已经完成的" in result.outcome.text
    assert len([c for c in core.calls if c["path"] == "/api/v1/tasks"]) == 1


# ---------------------------------------------------------------------------
# F6 幻觉 id
# ---------------------------------------------------------------------------


async def test_f6_hallucinated_id_never_reaches_core(runtime, core) -> None:
    script = [
        call("task.update", task_id="t_999", version=1, patch={"priority": "high"}),
        call("task.search", limit=5),  # 模型下一步改用了 search
        say("我先查到了真实的任务。"),
    ]
    result = await run_script(runtime, script, "把报告标记完成")

    assert result.outcome.ok
    # 关键断言：请求根本没发出去
    assert not [c for c in core.calls if c["method"] == "PATCH"]
    failed = [d for d in result.of("tool_result") if d["ok"] is False]
    assert failed and "不在本轮可见" in failed[0]["summary"]
    # 模型随后确实改用了 search
    assert any(c["path"] == "/api/v1/tasks" for c in core.calls)


# ---------------------------------------------------------------------------
# F7 危险操作
# ---------------------------------------------------------------------------


async def test_f7_delete_requires_confirmation(runtime, core) -> None:
    script = [call("task.search", limit=5), call("task.delete", task_id="t_001")]
    result = await run_script(runtime, script, "把买牛奶删掉")

    assert result.outcome.confirm is not None
    payload = result.of("confirm")[0]
    assert payload["confirmToken"].startswith("cf_")
    assert payload["plan"]["snapshot"]["id"] == "t_001"
    # 数据没被删
    assert "t_001" in core.tasks
    assert not [c for c in core.calls if c["method"] == "DELETE"]


async def test_f7_confirm_token_is_single_use(runtime, core) -> None:
    script = [call("task.search", limit=5), call("task.delete", task_id="t_001")]
    result = await run_script(runtime, script, "把买牛奶删掉")
    token = result.outcome.confirm["confirmToken"]

    from cadence.guard import validators

    tool = runtime.registry.require("task.delete")
    pending = runtime.confirms.take(token)
    assert pending is not None
    parsed = validators.parse_args(pending.tool, tool.args_model, pending.args)
    await tool.handler(parsed, result.ctx)
    assert "t_001" not in core.tasks
    # 第二次一定失败
    assert runtime.confirms.take(token) is None


# ---------------------------------------------------------------------------
# F8 上下文超限
# ---------------------------------------------------------------------------


async def test_f8_context_over_budget_triggers_compression(runtime, core) -> None:
    runtime.settings.max_context_tokens = 30
    for i in range(8):
        runtime.sessions.get("s_test").add_turn("user", f"第{i}轮的废话" * 20)
        runtime.sessions.get("s_test").add_turn("assistant", f"第{i}轮的回答" * 20)

    script = [
        call("task.search", limit=5),
        call("task.search", keyword="牛奶"),
        say("好了。"),
    ]
    result = await run_script(runtime, script, "看看任务")

    assert result.outcome.ok
    names = [s.name for s in result.ctx.trace.steps]
    assert "guard.compress" in names, "超过上下文预算必须压缩"


# ---------------------------------------------------------------------------
# F9 模型超时 → 降级
# ---------------------------------------------------------------------------


async def test_f9_timeout_retries_then_degrades(runtime, core) -> None:
    script = [fail(LLMTimeout("上游超时")), fail(LLMTimeout("上游超时")), say("降级模式下的回答")]
    result = await run_script(runtime, script, "帮我准备面试", tier="reason")

    assert result.outcome.degraded is True
    assert result.outcome.degrade_reason.startswith("reason_model_unavailable")
    assert any(d.get("degraded") for d in result.of("error"))


async def test_f9_bad_output_is_retried_once(runtime) -> None:
    """F1 的重试语义：一次非法输出仍然被重试救回来。"""
    from cadence.llm.fake import FakeLLM

    assert isinstance(runtime.llm, FakeLLM)
    result = await run_script(runtime, [bad_json(), say("这次好了。")], "看看任务")
    assert result.outcome.ok
    assert result.outcome.text == "这次好了。"
