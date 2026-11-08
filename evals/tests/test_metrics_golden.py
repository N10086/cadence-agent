"""golden 单测：固定 trace JSONL → 13 个指标的具体数值（L4-7）。

不是"大于 0"，而是每个指标一个可复算的常量。任何公式改动都会在这里立刻暴露。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.metrics import METRIC_NAMES, compute_metrics

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "golden_trace.jsonl"


def _rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_metric_names_are_frozen_to_thirteen() -> None:
    assert len(METRIC_NAMES) == 13
    assert "steps_avg" not in METRIC_NAMES


def test_golden_trace_metrics_have_exact_values() -> None:
    report = compute_metrics(_rows())

    assert report["task_success"] == pytest.approx(0.75)
    assert report["tool_name_accuracy"] == pytest.approx(6 / 7)
    assert report["tool_seq_f1"] == pytest.approx(12 / 13)
    assert report["arg_field_accuracy"] == pytest.approx(9 / 12)
    assert report["date_parse_accuracy"] == pytest.approx(0.5)
    assert report["hallucinated_id_rate"] == pytest.approx(0.25)
    assert report["unsafe_action_blocked_rate"] == pytest.approx(1.0)
    assert report["injection_resist_rate"] == pytest.approx(1.0)
    assert report["clarification_rate"] == pytest.approx(1.0)
    assert report["latency_p50"] == pytest.approx(1000.0)
    assert report["latency_p95"] == pytest.approx(1900.0)
    assert report["cost_per_turn"] == pytest.approx(0.000875)
    assert report["degraded_rate"] == pytest.approx(0.25)

    assert set(report) == set(METRIC_NAMES)


def test_busy_turn_is_excluded_from_latency_but_not_turn_count() -> None:
    rows = _rows()
    # golden-4 的 ended_by=busy，latency_ms=9000；它不能把 p95 拉高。
    assert report_latency(rows) < 9000
    # 但它仍然是 4 轮之一：task_success / cost_per_turn 的分母不变。
    assert compute_metrics(rows)["task_success"] == pytest.approx(0.75)


def report_latency(rows: list[dict]) -> float:
    return compute_metrics(rows)["latency_p95"]
