"""多步：S1 剧本「完成买牛奶，再建一个周五前交报告的高优先级任务」。"""

from __future__ import annotations

from cadence.llm.fake import call, say

from conftest import run_script


async def test_s1_multi_step_scenario(runtime, core) -> None:
    """验收场景 7.1「多步意图」的直译。"""
    script = [
        call("task.search", keyword="买牛奶"),
        call("task.complete", task_id="t_001", version=2),
        call("task.create", title="交报告", priority="high", due_at="2026-11-13T18:00:00+08:00"),
        call(
            "meta.finish",
            message="已经把「买牛奶」标记完成，并新建了周五前交报告的 HIGH 任务。",
            actions_taken=["完成任务「买牛奶」", "创建任务「交报告」"],
        ),
    ]

    result = await run_script(runtime, script, "把买牛奶标记完成，然后建一个周五前交报告的高优先级任务")

    assert result.outcome.ok
    # 最终状态（不是只看工具调用）
    assert core.tasks["t_001"]["status"] == "DONE"
    created = [t for t in core.tasks.values() if t["title"] == "交报告"]
    assert created and created[0]["priority"] == "HIGH"
    # 步骤数 ≤ 5
    assert result.outcome.steps <= 5
    # 工具序列（meta.finish 是控制流，会出现在最后）
    names = [d["name"] for d in result.of("tool_call")]
    assert names[:3] == ["task.search", "task.complete", "task.create"]
    assert names[-1] == "meta.finish"


async def test_finish_claims_are_checked(runtime, core) -> None:
    """模型声称做了没做的事 → 被 guard 标出来，回复里加免责说明。"""
    script = [
        call("task.search", limit=1),
        call(
            "meta.finish",
            message="都帮你排好了。",
            actions_taken=["已经把六条任务全部重排完毕"],
        ),
    ]
    result = await run_script(runtime, script, "帮我排一下")

    assert result.outcome.ok
    assert "没有执行成功" in result.outcome.text


async def test_read_only_claims_are_not_flagged(runtime, core) -> None:
    """只读描述（"我查了一下"）不该被当成编造 —— 读操作不进 actions_taken。"""
    script = [
        call("task.search", limit=5),
        call("meta.finish", message="我查了一下，你有两个任务。", actions_taken=["查询了今天的任务"]),
    ]
    result = await run_script(runtime, script, "看看任务")
    assert result.outcome.ok
    assert "没有执行成功" not in result.outcome.text


async def test_search_creates_visible_ids_for_later_steps(runtime, core) -> None:
    """白名单的可见集合来自 search 结果，后续步骤才能用这些 id。"""
    script = [
        call("task.search", limit=10),
        call("task.complete", task_id="t_002", version=1),
        say("完成。"),
    ]
    result = await run_script(runtime, script, "把写周报标记完成")
    assert result.outcome.ok
    assert core.tasks["t_002"]["status"] == "DONE"
