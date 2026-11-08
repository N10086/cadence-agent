"""Markdown 报告（L4-4）。

固定六张表：

1. 总览（含 13 个指标 + `steps_avg` 诊断行）
2. Token 与成本
3. 每工具调用
4. 动作 vs 回答时延（含 T_llm / T_tool / T_queue / T_overhead 拆分）
5. 分组通过率（分组聚合）
6. 失败明细 · 失败用例 Top 5（带 trace_id）

报告头四行必须存在：数据集版本 / 模型 / 模式（live|fake）/ 缓存命中率。
fake 模式会在头部明确写"不是能力结论"——这条是红线，不靠读者自己猜。
"""

from __future__ import annotations

from typing import Any

from .metrics import (
    METRIC_NAMES,
    compute_metrics,
    count_expected_fields,
    count_matched_fields,
    group_accuracy,
    lcs_pairs,
    percentile,
)


def _get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _ms(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}ms"


def _metric_text(name: str, value: Any) -> str:
    if value is None:
        return "—"
    if name in {"latency_p50", "latency_p95"}:
        return _ms(float(value))
    if name == "cost_per_turn":
        return f"¥{float(value):.6f}"
    return _pct(float(value))


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


def build_report(summary: Any, *, generated_at: str | None = None) -> dict[str, Any]:
    rows = list(getattr(summary, "rows", []) or [])
    metrics = compute_metrics(rows)
    groups = group_accuracy(rows)
    failures = [row for row in rows if not bool(_get(row, "passed", False))]

    tokens_in = sum(int(_get(row, "tokens_in", 0) or 0) for row in rows)
    tokens_out = sum(int(_get(row, "tokens_out", 0) or 0) for row in rows)
    cost = sum(float(_get(row, "cost_yuan", 0.0) or 0.0) for row in rows)
    turns = len(rows)

    latencies = [
        row
        for row in rows
        if not bool(_get(row, "latency_excluded", False))
        and _get(row, "ended_by", "") not in {"busy", "timeout"}
    ]
    total_latency = sum(float(_get(row, "latency_ms", 0) or 0) for row in latencies)
    llm_ms = sum(int(_get(row, "llm_ms", 0) or 0) for row in latencies)
    tool_ms = sum(int(_get(row, "tool_ms", 0) or 0) for row in latencies)
    queue_ms = sum(float(_get(row, "queue_wait_ms", 0) or 0) for row in latencies)
    overhead = max(0.0, total_latency - llm_ms - tool_ms - queue_ms)

    def _share(value: float) -> float:
        return (value / total_latency) if total_latency else 0.0

    action_rows = [row for row in latencies if "action" in (_get(row, "tags", []) or [])]
    answer_rows = [row for row in latencies if "answer" in (_get(row, "tags", []) or [])]

    return {
        "generated_at": generated_at or "",
        "dataset_version": getattr(summary, "dataset_version", ""),
        "model": _first_model(rows) or getattr(summary, "model", "") or "",
        "mode": getattr(summary, "mode", ""),
        "llm_mode": getattr(summary, "llm_mode", ""),
        "core_mode": getattr(summary, "core_mode", ""),
        "cache_hit_rate": float(getattr(summary, "cache_hit_rate", 0.0) or 0.0),
        "repeat": int(getattr(summary, "repeat", 1) or 1),
        "prompt_version": _first(rows, "prompt_version") or "",
        "elapsed_s": float(getattr(summary, "elapsed_s", 0.0) or 0.0),
        "metrics": metrics,
        "groups": groups,
        "failures": failures,
        "rows": rows,
        "turns": turns,
        "strict_passed": sum(1 for row in rows if bool(_get(row, "passed", False))),
        "tokens": {
            "in": tokens_in,
            "out": tokens_out,
            "per_turn": round((tokens_in + tokens_out) / turns, 1) if turns else 0.0,
        },
        "cost": {
            "total": round(cost, 6),
            "per_turn": round(cost / turns, 6) if turns else 0.0,
        },
        "diagnostics": {
            "steps_avg": round(sum(int(_get(row, "steps", 0) or 0) for row in rows) / turns, 2)
            if turns else 0.0,
            "busy_count": sum(1 for row in rows if _get(row, "ended_by", "") == "busy"),
            "timeout_count": sum(1 for row in rows if _get(row, "ended_by", "") == "timeout"),
            "cache_hits": sum(1 for row in rows if bool(_get(row, "cache_hit", False))),
        },
        "latency": {
            "action": {
                "n": len(action_rows),
                "p50": percentile([float(_get(row, "latency_ms", 0)) for row in action_rows], 50),
                "p95": percentile([float(_get(row, "latency_ms", 0)) for row in action_rows], 95),
            },
            "answer": {
                "n": len(answer_rows),
                "p50": percentile([float(_get(row, "latency_ms", 0)) for row in answer_rows], 50),
                "p95": percentile([float(_get(row, "latency_ms", 0)) for row in answer_rows], 95),
            },
            "split": {
                "llm": llm_ms,
                "tool": tool_ms,
                "queue": round(queue_ms, 3),
                "overhead": round(overhead, 3),
                "llm_pct": _share(llm_ms),
                "tool_pct": _share(tool_ms),
                "queue_pct": _share(queue_ms),
                "overhead_pct": _share(overhead),
            },
        },
        "tool_stats": _tool_stats(rows),
    }


def _first(rows: list[Any], key: str) -> Any:
    for row in rows:
        value = _get(row, key, "")
        if value:
            return value
    return ""


def _first_model(rows: list[Any]) -> str:
    return str(_first(rows, "model") or "")


def _tool_stats(rows: list[Any]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        expected = [call for call in (_get(row, "tool_calls_expected", []) or []) if call.get("name") != "meta.finish"]
        actual = [call for call in (_get(row, "tool_calls_actual", []) or []) if call.get("name") != "meta.finish"]
        # 与 metrics 同一套归一化：额外 search 不参与 per-tool 统计的"期望"侧
        expected_searches = sum(1 for call in expected if call.get("name") == "task.search")
        normalized: list[dict[str, Any]] = []
        for call in actual:
            if call.get("name") == "task.search":
                if expected_searches <= 0:
                    continue
                expected_searches -= 1
            normalized.append(call)
        pairs = {i: j for i, j in lcs_pairs(expected, normalized)}

        for index, call in enumerate(expected):
            name = str(call.get("name"))
            entry = stats.setdefault(name, _empty_tool())
            entry["expected"] += 1
            args = call.get("args") or {}
            entry["fields"] += count_expected_fields(args)
            if index in pairs:
                entry["name_matched"] += 1
                entry["hits"] += count_matched_fields(args, normalized[pairs[index]].get("args") or {})

        for call in actual:
            name = str(call.get("name"))
            entry = stats.setdefault(name, _empty_tool())
            entry["calls"] += 1
            if bool(call.get("ok", False)):
                entry["success"] += 1

        for step in _get(row, "tool_steps", []) or []:
            if not bool(step.get("ok", True)):
                continue
            name = str(step.get("name"))
            entry = stats.setdefault(name, _empty_tool())
            entry["durations"].append(float(step.get("duration_ms") or 0))

    for entry in stats.values():
        calls = entry["calls"]
        entry["success_rate"] = (entry["success"] / calls) if calls else 0.0
        entry["name_accuracy"] = (entry["name_matched"] / entry["expected"]) if entry["expected"] else 0.0
        entry["arg_accuracy"] = (entry["hits"] / entry["fields"]) if entry["fields"] else 0.0
        entry["avg_ms"] = round(sum(entry["durations"]) / len(entry["durations"]), 1) if entry["durations"] else 0.0
        entry["p95_ms"] = percentile(entry["durations"], 95)
    return dict(sorted(stats.items(), key=lambda item: (-item[1]["calls"], item[0])))


def _empty_tool() -> dict[str, Any]:
    return {
        "calls": 0,
        "success": 0,
        "expected": 0,
        "name_matched": 0,
        "fields": 0,
        "hits": 0,
        "durations": [],
        "success_rate": 0.0,
        "name_accuracy": 0.0,
        "arg_accuracy": 0.0,
        "avg_ms": 0.0,
        "p95_ms": 0.0,
    }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report.get("metrics") or {}
    groups = report.get("groups") or {}
    failures = list(report.get("failures") or [])
    rows = list(report.get("rows") or [])
    diagnostics = report.get("diagnostics") or {}
    latency = report.get("latency") or {}
    tokens = report.get("tokens") or {}
    cost = report.get("cost") or {}
    tool_stats = report.get("tool_stats") or {}
    mode = str(report.get("mode") or "—")
    turns = int(report.get("turns") or len(rows))
    passed = int(report.get("strict_passed") or 0)

    lines: list[str] = []
    lines.append("# Cadence 工具调用评测报告")
    lines.append("")
    lines.append(f"- 数据集版本：{report.get('dataset_version') or '—'}（{turns} 条）")
    lines.append(f"- 模型：{report.get('model') or '—'}")
    lines.append(f"- 模式：{mode}")
    lines.append(f"- 缓存命中率：{_pct(float(report.get('cache_hit_rate') or 0.0))}")
    lines.append("")
    if mode == "fake":
        lines.append(
            "> ⚠️ **fake 模式：下面的数字只证明评测流水线能跑通，不是模型能力结论。**"
            "真实数字必须在有 Core + API key 的桌面上用 `--llm live --core live` 重跑。"
        )
    elif mode != "live":
        lines.append(f"> ⚠️ 混合模式（{mode}）：不是完整的 live 链路，数字不能当作能力结论。")
    else:
        lines.append("> 模式为 live：Agent/Core 均为真实服务。")
    lines.append("")

    # ---- 表 1 ----
    lines.append("## 表1 总览")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 严格通过 | {passed}/{turns} ({_pct(passed / turns if turns else 0.0)}) |")
    lines.append(f"| 任务完成率 `task_success` | {_pct(metrics.get('task_success'))} |")
    lines.append(f"| 平均步数 `steps_avg`（诊断行，不计入 13 指标） | {diagnostics.get('steps_avg', 0.0)} |")
    lines.append(f"| busy 轮数（已排除出时延） | {diagnostics.get('busy_count', 0)} |")
    lines.append(f"| timeout 轮数（已排除出时延） | {diagnostics.get('timeout_count', 0)} |")
    lines.append("")
    lines.append("**13 个指标**")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    for name in METRIC_NAMES:
        lines.append(f"| `{name}` | {_metric_text(name, metrics.get(name))} |")
    lines.append("")

    # ---- 表 2 ----
    lines.append("## 表2 Token 与成本")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 每轮 token | {tokens.get('per_turn', 0.0)} |")
    lines.append(f"| 输入 token 合计 | {tokens.get('in', 0)} |")
    lines.append(f"| 输出 token 合计 | {tokens.get('out', 0)} |")
    lines.append(f"| 每轮成本 | ¥{float(cost.get('per_turn') or 0.0):.6f} |")
    lines.append(f"| 总成本 | ¥{float(cost.get('total') or 0.0):.6f} |")
    lines.append(f"| 缓存命中 | {diagnostics.get('cache_hits', 0)} 轮 |")
    lines.append("")

    # ---- 表 3 ----
    lines.append("## 表3 每工具调用")
    lines.append("")
    lines.append("| tool | calls | success | name_acc | arg_acc | avg ms | p95 ms |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    if tool_stats:
        for name, entry in tool_stats.items():
            lines.append(
                f"| `{name}` | {entry.get('calls', 0)} | {_pct(entry.get('success_rate'))} | "
                f"{_pct(entry.get('name_accuracy'))} | {_pct(entry.get('arg_accuracy'))} | "
                f"{entry.get('avg_ms', 0)} | {entry.get('p95_ms', 0)} |"
            )
    else:
        lines.append("| — | 0 | — | — | — | — | — |")
    lines.append("")

    # ---- 表 4 ----
    action = latency.get("action") or {}
    answer = latency.get("answer") or {}
    split = latency.get("split") or {}
    lines.append("## 表4 动作 vs 回答时延")
    lines.append("")
    lines.append("| 类别 | n | p50 | p95 |")
    lines.append("|---|---:|---:|---:|")
    lines.append(f"| 动作类（tag=action） | {action.get('n', 0)} | {_ms(action.get('p50'))} | {_ms(action.get('p95'))} |")
    lines.append(f"| 回答类（tag=answer） | {answer.get('n', 0)} | {_ms(answer.get('p50'))} | {_ms(answer.get('p95'))} |")
    lines.append("")
    lines.append(
        f"拆分：T_llm {_pct(split.get('llm_pct'))} · T_tool {_pct(split.get('tool_pct'))} · "
        f"T_queue {_pct(split.get('queue_pct'))} · T_overhead {_pct(split.get('overhead_pct'))}"
    )
    lines.append("")

    # ---- 表 5 ----
    lines.append("## 表5 分组通过率（分组聚合）")
    lines.append("")
    lines.append("| 分组 | 通过率 |")
    lines.append("|---|---:|")
    group_order = ["single", "multi", "ambiguous", "adversarial", "domain", "regression"]
    for group in group_order:
        if group in groups:
            lines.append(f"| {group} | {_pct(groups.get(group))} |")
    if not groups:
        lines.append("| — | — |")
    lines.append("")

    # ---- 表 6 ----
    lines.append("## 表6 失败明细 · 失败用例 Top 5")
    lines.append("")
    if failures:
        lines.append("| # | 用例 | 分组 | 原因 | trace_id |")
        lines.append("|---:|---|---|---|---|")
        for index, failure in enumerate(failures[:5], start=1):
            case_id = _get(failure, "case_id", _get(failure, "id", "?"))
            group = _get(failure, "group", "?")
            reason = _get(failure, "failed_reason", "") or _get(failure, "error", "") or "失败"
            trace_id = _get(failure, "trace_id", "") or "—"
            lines.append(f"| {index} | `{case_id}` | {group} | {reason} | `{trace_id}` |")
    else:
        lines.append("无失败用例。")
    lines.append("")

    lines.append("---")
    lines.append(
        f"生成时间：{report.get('generated_at') or '—'} · 数据集 `{report.get('dataset_version') or '—'}` · "
        f"prompt `{report.get('prompt_version') or '—'}` · repeat={report.get('repeat', 1)}"
    )
    return "\n".join(lines) + "\n"


__all__ = ["build_report", "render_markdown"]
