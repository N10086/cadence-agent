"""单轮：一次工具调用 → 一句回复。"""

from __future__ import annotations

from cadence.llm.fake import call, say

from conftest import run_script


async def test_search_then_answer(runtime, core) -> None:
    result = await run_script(
        runtime,
        [call("task.search", limit=5), say("你手上有 2 个任务。")],
        "看看我今天有什么任务",
    )

    assert result.outcome.ok
    assert "2 个任务" in result.outcome.text
    assert [e for e in result.of("tool_call")][0]["name"] == "task.search"
    assert result.of("tool_result")[0]["ok"] is True
    assert core.calls[0]["path"] == "/api/v1/tasks"
    assert result.outcome.steps == 2


async def test_tags_and_headers_are_forwarded(runtime, core) -> None:
    await run_script(runtime, [call("task.search", limit=1), say("ok")], "看看任务")
    assert core.calls[0]["actor"] == "agent"
    assert core.calls[0]["trace_id"] == "tr_test"


async def test_token_events_reconstruct_text(runtime) -> None:
    result = await run_script(runtime, [say("你好，今天有三件事要做。")], "你好")
    assert result.text_tokens == "你好，今天有三件事要做。"


async def test_no_tool_call_is_plain_answer(runtime, core) -> None:
    result = await run_script(runtime, [say("我在。")], "你好")
    assert result.outcome.ok
    assert core.calls == []
    assert result.outcome.steps == 1
