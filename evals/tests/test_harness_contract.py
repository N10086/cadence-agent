"""L4 harness 契约测试（不解除冻结 spec 的 skip）。

覆盖：数据集 35 条 / 4 套配置可加载 / 缓存 / 报告六表 / CLI 未知用例 / fake 流水线。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.cache import RunCache
from evals.cli import main as cli_main
from evals.config import load_configs
from evals.dataset import GROUPS, load_dataset, validate_case
from evals.report import render_markdown
from evals.runner import run_case

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evals" / "datasets" / "v1"
CONFIGS = ROOT / "evals" / "configs"


def test_dataset_has_35_cases_in_six_groups() -> None:
    ds = load_dataset(DATASET)
    assert ds.version == "v1"
    assert len(ds.cases) == 35
    assert set(ds.group_counts) == set(GROUPS)
    assert ds.group_counts == {
        "single": 14,
        "multi": 6,
        "ambiguous": 5,
        "adversarial": 5,
        "domain": 0,
        "regression": 5,
    }


def test_each_case_has_required_assertions() -> None:
    ds = load_dataset(DATASET)
    for case in ds.cases:
        assert case.id
        assert case.group in GROUPS
        assert case.input
        assert case.setup.get("tasks") is not None
        assert case.expect.final_state is not None
        assert case.expect.must_not_call is not None
        assert case.tags


def test_validate_case_rejects_missing_final_state() -> None:
    with pytest.raises(ValueError):
        validate_case({"id": "bad", "group": "single", "input": "x"})


def test_write_tool_with_unchanged_final_state_requires_allow_noop() -> None:
    """B4 的教训：写工具 + 终态与 setup 完全一致 = 空转，必须显式 allow_noop。"""
    case = {
        "id": "B4-old",
        "group": "single",
        "input": "读论文我开始了。",
        "setup": {"tasks": [{"alias": "论文", "title": "读论文", "status": "DOING"}]},
        "expect": {
            "tool_calls": [
                {"name": "task.update", "args": {"task_id": "$论文", "patch": {"status": "doing"}}}
            ],
            "final_state": {"count_delta": 0, "tasks": [{"id": "$论文", "status": "DOING"}]},
            "must_not_call": ["task.complete"],
        },
        "tags": ["B", "action"],
    }
    with pytest.raises(ValueError, match="allow_noop"):
        validate_case(case)
    case["allow_noop"] = True
    validate_case(case)


def test_f4_idempotent_case_is_explicitly_allow_noop() -> None:
    ds = load_dataset(DATASET)
    f4 = ds.by_id("F4")
    assert f4 is not None and f4.allow_noop is True


def test_four_ablation_configs_load() -> None:
    configs = load_configs(CONFIGS)
    assert [config.name for config in configs] == ["B0", "B1", "B2", "B3"]
    assert configs[0].features.tool_contract is False
    assert configs[-1].features.guard_id_whitelist is True


def test_cache_put_get_roundtrip() -> None:
    cache_dir = ROOT / ".tmp" / "pytest-evals"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = RunCache(cache_dir / "cache.json")
    cache.clear()
    cache.put(model="m", prompt_hash="p", input="牛奶", output={"ok": True})
    assert cache.get(model="m", prompt_hash="p", input="牛奶") == {"ok": True}
    assert cache.get(model="m", prompt_hash="p", input="别的") is None
    assert cache.hits == 1 and cache.misses == 1


def test_report_has_six_tables_and_header() -> None:
    md = render_markdown(
        {
            "dataset_version": "v1",
            "model": "fake-model",
            "mode": "fake",
            "cache_hit_rate": 0.0,
            "metrics": {"task_success": 0.8},
            "failures": [{"case_id": f"F{i}", "group": "multi", "trace_id": f"tr_{i}"} for i in range(7)],
            "groups": {"single": 0.9, "multi": 0.7},
        }
    )
    assert "表1 总览" in md
    assert "表2 Token 与成本" in md
    assert "表3 每工具调用" in md
    assert "表4 动作 vs 回答时延" in md
    assert "表5 分组通过率" in md
    assert "表6 失败明细 · 失败用例 Top 5" in md
    assert "失败用例 Top 5" in md
    assert "single" in md and "multi" in md
    assert "数据集版本" in md and "模型" in md and "模式" in md and "缓存命中率" in md
    assert md.count("tr_") >= 5


def test_cli_unknown_case_returns_nonzero_and_lists_candidates(capsys) -> None:
    rc = cli_main(["--case", "multi-007", "--dataset", str(DATASET)])
    captured = capsys.readouterr()
    assert rc != 0
    assert "未知用例" in captured.err
    assert "候选" in captured.err


def test_fake_runner_pipeline_passes_a_read_case() -> None:
    ds = load_dataset(DATASET)
    result = run_case(ds.by_id("A1"))
    assert result.passed, result.error
    assert result.tool_calls_ok and result.final_state_ok and result.must_not_call_ok
    assert result.core_task_count == 8
    assert result.trace_id.startswith("tr_")
