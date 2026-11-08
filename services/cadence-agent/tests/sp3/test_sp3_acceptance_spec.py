"""SP3 验收规格（默认 skip，等 cadence-brain 实现后启用）。

这不是"跑不过的测试"，而是**先写好的验收合同**：
- 求解器必须是纯函数：同输入同输出，不碰 LLM/HTTP/DB；
- 硬约束 100% 满足，pin 不动，依赖顺序正确；
- `cannot_fit` 必须诚实，不能硬塞；
- 50 任务求解 < 2s；
- 分解 <= 7 项，每项有工时，用户拒绝不落库；
- reschedule 的 dry-run diff 必须含 moved/kept/cannot_fit，apply 前校验 version。

实现落地后删掉模块级 skip 即可。未实现时整套 skip，不污染现有测试结果。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skip(reason="SP3 未实现：solver/decompose/proactive 仍是桩；实现后删除本 skip")

TZ8 = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 8, 10, 30, tzinfo=TZ8)


def _problem():
    from cadence.brain.solver.model import Constraints, Problem, TaskSnapshot, TimeWindow

    tasks = [
        TaskSnapshot(
            id="t1", title="交报告", priority="urgent", estimate_min=120,
            due_at=NOW + timedelta(days=2), pinned=False, depends_on=[],
            current_slot=TimeWindow(NOW, NOW + timedelta(hours=1)),
        ),
        TaskSnapshot(
            id="t2", title="客户演示", priority="high", estimate_min=60,
            due_at=NOW + timedelta(days=1), pinned=True, depends_on=[],
            current_slot=TimeWindow(NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1)),
        ),
        TaskSnapshot(
            id="t3", title="写周报", priority="normal", estimate_min=60,
            due_at=NOW + timedelta(days=3), pinned=False, depends_on=["t1"],
            current_slot=TimeWindow(NOW + timedelta(hours=2), NOW + timedelta(hours=3)),
        ),
    ]
    return Problem(
        tasks=tasks,
        busy=[],
        working_hours=[TimeWindow(
            NOW.replace(hour=9, minute=0), NOW.replace(hour=18, minute=0)
        )],
        constraints=Constraints(
            unavailable=[],
            movable="all",
            pinned={"t2"},
            horizon=(NOW.date(), (NOW + timedelta(days=14)).date()),
            hard_deadlines=True,
        ),
    )


def test_solver_is_pure_and_deterministic() -> None:
    from cadence.brain.solver import solve

    p = _problem()
    a = solve(p)
    b = solve(p)
    assert a.placements == b.placements
    assert a.moved == b.moved
    assert a.cannot_fit == b.cannot_fit


def test_solver_does_not_use_network_or_llm() -> None:
    import inspect
    import cadence.brain.solver as solver

    src = inspect.getsource(solver)
    for forbidden in ("openai", "deepseek", "httpx", "requests", "sqlalchemy", "psycopg"):
        assert forbidden not in src.lower(), f"solver/ 不应出现 {forbidden}"


def test_pinned_task_never_moves() -> None:
    from cadence.brain.solver import solve

    sol = solve(_problem())
    assert "t2" in sol.kept
    assert sol.placements["t2"] == _problem().tasks[1].current_slot


def test_dependency_order_is_respected() -> None:
    from cadence.brain.solver import solve

    sol = solve(_problem())
    assert sol.placements["t3"].start >= sol.placements["t1"].end


def test_unavailable_window_is_never_used() -> None:
    from cadence.brain.solver import solve
    from cadence.brain.solver.model import TimeWindow

    p = _problem()
    blocked = TimeWindow(NOW + timedelta(hours=1), NOW + timedelta(hours=6))
    p = p.__class__(tasks=p.tasks, busy=p.busy, working_hours=p.working_hours,
                    constraints=p.constraints.__class__(
                        unavailable=[blocked], movable="all", pinned={"t2"},
                        horizon=p.constraints.horizon, hard_deadlines=True))
    sol = solve(p)
    for slot in sol.placements.values():
        assert slot.end <= blocked.start or slot.start >= blocked.end


def test_cannot_fit_is_honest() -> None:
    from cadence.brain.solver import solve
    from cadence.brain.solver.model import Constraints, Problem, TaskSnapshot, TimeWindow

    impossible = TaskSnapshot(
        id="big", title="不可能完成的任务", priority="urgent", estimate_min=480,
        due_at=NOW + timedelta(hours=1), pinned=False, depends_on=[],
        current_slot=TimeWindow(NOW, NOW + timedelta(minutes=60)),
    )
    p = Problem(
        tasks=[impossible], busy=[],
        working_hours=[TimeWindow(NOW, NOW + timedelta(hours=1))],
        constraints=Constraints(
            unavailable=[], movable="all", pinned=set(),
            horizon=(NOW.date(), NOW.date()), hard_deadlines=True,
        ),
    )
    sol = solve(p)
    assert any(x.task_id == "big" for x in sol.cannot_fit)
    assert "big" not in sol.placements


def test_50_tasks_solve_under_two_seconds() -> None:
    from cadence.brain.solver import solve
    from cadence.brain.solver.model import Constraints, Problem, TaskSnapshot, TimeWindow

    tasks = [
        TaskSnapshot(
            id=f"t{i}", title=f"任务{i}", priority="normal", estimate_min=30,
            due_at=NOW + timedelta(days=7), pinned=False, depends_on=[],
            current_slot=TimeWindow(NOW, NOW + timedelta(minutes=30)),
        )
        for i in range(50)
    ]
    p = Problem(
        tasks=tasks, busy=[],
        working_hours=[TimeWindow(NOW.replace(hour=9), NOW.replace(hour=18))],
        constraints=Constraints(
            unavailable=[], movable="all", pinned=set(),
            horizon=(NOW.date(), (NOW + timedelta(days=14)).date()), hard_deadlines=True,
        ),
    )
    sol = solve(p)
    assert sol.solve_ms < 2000


def test_churn_is_better_than_naive_shift() -> None:
    from cadence.brain.solver import solve

    sol = solve(_problem())
    # 暴力顺延会把所有非 pinned 任务都移动；PRD 要求 moved <= 0.6 * 暴力移动数
    naive_moved = len([t for t in _problem().tasks if not t.pinned])
    assert len(sol.moved) <= 0.6 * naive_moved


# ---------------------------------------------------------------------
# 分解
# ---------------------------------------------------------------------


async def test_decompose_at_most_seven_children() -> None:
    from cadence.brain.decompose import decompose

    result = await decompose("准备下周的面试", now=NOW)
    assert 2 <= len(result.children) <= 7
    for child in result.children:
        assert child["title"]
        assert 5 <= child["estimate_min"] <= 480


async def test_decompose_has_dependency_edges() -> None:
    from cadence.brain.decompose import decompose

    result = await decompose("准备下周的面试", now=NOW)
    assert any(child.get("depends_on") for child in result.children)


# ---------------------------------------------------------------------
# 主动建议
# ---------------------------------------------------------------------


async def test_proactive_returns_triggered_suggestions() -> None:
    from cadence.brain.proactive import suggest

    tasks = [{"id": "t1", "status": "TODO", "due_at": (NOW - timedelta(days=1)).isoformat()}]
    suggestions = await suggest(tasks=tasks, now=NOW)
    assert suggestions
    assert all(s.kind and s.message and s.task_ids for s in suggestions)


# ---------------------------------------------------------------------
# reschedule / apply
# ---------------------------------------------------------------------


async def test_reschedule_dry_run_returns_three_groups() -> None:
    from cadence.brain.solver.reschedule import reschedule

    plan = await reschedule(
        tasks=[{"id": "t1", "title": "写周报", "estimate_min": 60, "pinned": False}],
        unavailable=[{"start": NOW.isoformat(), "end": (NOW + timedelta(days=3)).isoformat()}],
        movable="all",
        pinned_task_ids=[],
        horizon_days=14,
        now=NOW,
    )
    assert set(plan) >= {"plan_id", "moved", "kept", "cannot_fit", "hard_constraints_satisfied"}
    assert isinstance(plan["moved"], list)
    assert isinstance(plan["kept"], list)
    assert isinstance(plan["cannot_fit"], list)


async def test_apply_with_stale_version_returns_plan_stale() -> None:
    from cadence.runtime.errors import ErrorCode, ToolError
    from cadence.tools.plan_tools import PlanApplyArgs, _apply

    with pytest.raises(ToolError) as ei:
        await _apply(PlanApplyArgs(plan_id="p_x", confirm_token="cf_x"), ctx=None)
    assert ei.value.code is ErrorCode.PLAN_STALE
