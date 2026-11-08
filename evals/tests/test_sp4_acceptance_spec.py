"""SP4 验收规格（默认 skip，等 cadence-eval 实现后启用）。

仓库里没有 `evals/` 运行器、数据集、指标、报告，因此这里不是"可运行的评测"，
而是**先冻结的验收合同**。实现落地后：
1. 删除模块级 skip；
2. 按 `PRD/subprojects/SP4-cadence-eval.md` 的接口补 `evals/` 包；
3. 这些用例应直接变成 CI 的 `full` / `smoke` 入口。

为什么先写：SP4 的 PRD 自己说"评测必须先于功能存在"。
没有这些断言，后面做 SP3 时"改 prompt 是变好还是变坏"仍然只能靠感觉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(reason="SP4 未实现：evals/ 数据集、Runner、指标、报告均不存在")

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evals" / "datasets" / "v1"


# ---------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------


def test_dataset_has_125_cases_in_six_groups() -> None:
    from evals.dataset import load_dataset

    ds = load_dataset(DATASET)
    assert ds.version
    assert len(ds.cases) == 125
    assert set(ds.group_counts) == {
        "single", "multi", "ambiguous", "adversarial", "domain", "regression",
    }
    assert ds.group_counts == {
        "single": 45, "multi": 25, "ambiguous": 20,
        "adversarial": 15, "domain": 12, "regression": 8,
    }


def test_each_case_has_required_assertions() -> None:
    from evals.dataset import load_dataset

    ds = load_dataset(DATASET)
    for case in ds.cases:
        assert case.id
        assert case.group in {
            "single", "multi", "ambiguous", "adversarial", "domain", "regression",
        }
        assert case.input
        assert case.expect.final_state is not None, f"{case.id} 缺 final_state"
        assert case.expect.must_not_call is not None, f"{case.id} 缺 must_not_call"


def test_dataset_schema_rejects_bad_case() -> None:
    from evals.dataset import validate_case

    with pytest.raises(ValueError):
        validate_case({"id": "bad", "group": "single", "input": "x"})  # 缺 final_state


# ---------------------------------------------------------------------
# 沙箱与断言
# ---------------------------------------------------------------------


def test_case_runs_in_isolated_sandbox() -> None:
    from evals.runner import run_case
    from evals.dataset import load_dataset

    ds = load_dataset(DATASET)
    a = run_case(ds.cases[0])
    b = run_case(ds.cases[0])
    assert a.final_state == b.final_state
    # 两条用例之间不能残留上一条的任务/会话
    assert a.core_task_count == b.core_task_count


def test_three_assertion_types_all_must_pass() -> None:
    from evals.runner import CaseResult

    assert CaseResult(passed=True, tool_calls_ok=True, final_state_ok=True, must_not_call_ok=True).passed
    assert not CaseResult(passed=False, tool_calls_ok=False, final_state_ok=True, must_not_call_ok=True).passed
    assert not CaseResult(passed=False, tool_calls_ok=True, final_state_ok=False, must_not_call_ok=True).passed
    assert not CaseResult(passed=False, tool_calls_ok=True, final_state_ok=True, must_not_call_ok=False).passed


# ---------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------


def test_thirteen_metrics_are_computed() -> None:
    from evals.metrics import METRIC_NAMES

    assert len(METRIC_NAMES) == 13
    assert {"task_success", "tool_name_accuracy", "hallucinated_id_rate",
            "clarification_rate", "latency_p50", "latency_p95",
            "cost_per_turn", "degraded_rate"} <= set(METRIC_NAMES)


def test_hallucinated_id_rate_is_zero_when_guard_blocks() -> None:
    from evals.metrics import compute_metrics

    report = compute_metrics([{
        "case_id": "adv-1", "group": "adversarial", "passed": True,
        "tool_calls_expected": [], "tool_calls_actual": [],
        "final_state_ok": True, "must_not_call_ok": True,
        "guard_blocked": True, "latency_ms": 100, "cost_yuan": 0.001,
    }])
    assert report["hallucinated_id_rate"] == 0.0


def test_group_accuracy_is_reported() -> None:
    from evals.metrics import group_accuracy

    rows = [
        {"group": "single", "passed": True}, {"group": "single", "passed": False},
        {"group": "multi", "passed": True},
    ]
    acc = group_accuracy(rows)
    assert acc["single"] == 0.5
    assert acc["multi"] == 1.0


# ---------------------------------------------------------------------
# 缓存 / 报告 / 消融
# ---------------------------------------------------------------------


def test_cache_hit_rate_at_least_80_percent_on_second_run() -> None:
    from evals.cache import RunCache

    cache = RunCache()
    for i in range(10):
        cache.put(model="m", prompt_hash="p", input=f"i{i}", output=f"o{i}")
    hits = sum(cache.get(model="m", prompt_hash="p", input=f"i{i}") is not None for i in range(10))
    assert hits == 10


def test_ablation_has_four_configs() -> None:
    from evals.config import load_configs

    configs = load_configs(ROOT / "evals" / "configs")
    assert [c.name for c in configs] == ["B0", "B1", "B2", "B3"]
    assert configs[0].features.tool_contract is False
    assert configs[-1].features.guard_id_whitelist is True


def test_report_contains_top5_and_group_aggregation() -> None:
    from evals.report import render_markdown

    md = render_markdown({
        "dataset_version": "v1",
        "metrics": {"task_success": 0.8},
        "failures": [{"id": f"f{i}"} for i in range(7)],
        "groups": {"single": 0.9, "multi": 0.7},
    })
    assert "失败用例 Top 5" in md
    assert md.count("f") >= 5
    assert "single" in md and "multi" in md


def test_single_case_rerun_by_id() -> None:
    from evals.cli import main

    assert main(["--case", "multi-007", "--dataset", str(DATASET)]) == 0


# ---------------------------------------------------------------------
# 非功能
# ---------------------------------------------------------------------


def test_smoke_under_three_minutes_and_full_under_fifteen() -> None:
    from evals.runner import run_dataset

    smoke = run_dataset(DATASET, limit=20)
    assert smoke.elapsed_s < 180
    full = run_dataset(DATASET)
    assert full.elapsed_s < 900
    assert full.cost_yuan < 1.5
