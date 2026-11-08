"""从 runner 落下的 case.json 生成一份人可读的详细评测报告。

数据源：<run_root>/<case>/case.json（runner._save_case 写）
输出：evals/reports/YYYY-MM-DD-详细.md

用法：
    python scripts/report_detailed.py .tmp/evals/run-20261108-214700-d93b
    python scripts/report_detailed.py --latest
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from evals.metrics import value_matches  # noqa: E402

GROUP_NAMES = {
    "single": "单轮",
    "multi": "多步",
    "ambiguous": "歧义",
    "adversarial": "对抗",
    "regression": "边界",
}


def load_cases(run_root: Path) -> list[dict]:
    rows = []
    for path in run_root.rglob("case.json"):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    rows.sort(key=lambda r: (r.get("case", {}).get("id") or ""))
    return rows


def short(task_id: str) -> str:
    return (task_id or "")[:8] + "…" if len(task_id or "") > 10 else (task_id or "-")


def fmt_args(args) -> str:
    if not args:
        return "—"
    return json.dumps(args, ensure_ascii=False)


def alias_table(whitelist: dict) -> str:
    aliases = whitelist.get("aliases") or {}
    if not aliases:
        return "（无）"
    return "、".join(f"`{a}`={short(i)}" for a, i in aliases.items())


def steps_block(steps: list[dict]) -> list[str]:
    """把 LLM/TOOL/GUARD 序列整理成"每轮：思考 → 决策 → 行为"。"""
    lines: list[str] = []
    rounds = 0
    pending: dict | None = None

    def flush_pending():
        nonlocal pending
        if pending is None:
            return
        d = pending.get("detail") or {}
        text = (d.get("text") or "").strip()
        reasoning = (d.get("reasoning") or "").strip()
        if reasoning:
            lines.append(f"- 思考（reasoning）：{reasoning}")
        elif text:
            lines.append(f"- 思考（模型原话）：{text}")
        else:
            lines.append("- 思考：该步未返回 `reasoning_content`（模型直接给出工具调用）")
        tools = d.get("tool_calls") or []
        lines.append(f"- 决策：{'、'.join(f'`{t}`' for t in tools) if tools else '无工具调用（直接回答）'}")
        pending = None

    for step in steps:
        kind = step.get("kind")
        if kind == "LLM":
            flush_pending()
            name = str(step.get("name") or "")
            if name.startswith("llm.step"):
                rounds += 1
                lines.append(
                    f"\n**第 {rounds} 轮**（LLM {step.get('durationMs', 0)}ms，"
                    f"{step.get('tokensIn', 0)}→{step.get('tokensOut', 0)} tokens）"
                )
                pending = step
            elif name == "route.classify":
                d = step.get("detail") or {}
                lines.append(
                    f"- 路由：判定为 `{d.get('tier')}`（规则 `{d.get('rule')}`，{d.get('reason')}）"
                )
        elif kind == "TOOL":
            flush_pending()
            d = step.get("detail") or {}
            state = "成功" if step.get("ok", True) else "**失败**"
            summary = (d.get("summary") or "").strip() or "（无摘要）"
            lines.append(
                f"- 行为：`{step.get('name')}` {state}（{step.get('durationMs', 0)}ms）—— {summary}"
            )
        elif kind == "GUARD":
            d = step.get("detail") or {}
            name = str(step.get("name") or "")
            if name.startswith("guard."):
                note = d.get("reason") or d.get("note") or d.get("claims") or d.get("intent") or ""
                if name in ("guard.confirm_required", "guard.confirm_stale", "guard.confirm_ok",
                            "guard.whitelist", "guard.actions_unverified", "guard.classify"):
                    lines.append(f"- 护栏：`{name}` {json.dumps(note, ensure_ascii=False)}")
        elif kind == "DEGRADE":
            d = step.get("detail") or {}
            lines.append(f"- 降级：`{step.get('name')}` {json.dumps(d, ensure_ascii=False)}")
    flush_pending()
    return lines


def diff_calls(expected: list[dict], actual: list[dict]) -> list[str]:
    """把"工具调用不一致"落到具体字段，报告里才可读。"""
    remaining = [c for c in actual if c.get("name") != "meta.finish"]
    lines: list[str] = []
    for i, exp in enumerate(expected, 1):
        name = exp.get("name")
        idx = next((j for j, c in enumerate(remaining) if c.get("name") == name), None)
        if idx is None:
            lines.append(f"- 标准答案第 {i} 个调用 `{name}` 没有出现")
            continue
        act = remaining.pop(idx)
        for key, want in (exp.get("args") or {}).items():
            got = (act.get("args") or {}).get(key)
            if not value_matches(want, got):
                lines.append(
                    f"- `{name}.{key}`：期望 `{json.dumps(want, ensure_ascii=False)}`"
                    f"，实际 `{json.dumps(got, ensure_ascii=False)}`"
                )
    for extra in remaining:
        if extra.get("name") != "task.search":
            lines.append(f"- 多调用了 `{extra.get('name')}`")
    return lines


def case_section(row: dict, index: int) -> list[str]:
    case = row.get("case") or {}
    summary = row.get("summary") or {}
    result = row.get("result") or {}
    whitelist = row.get("whitelist") or {}
    ok = bool(result.get("passed"))
    mark = "✅ 通过" if ok else "❌ 失败"

    lines = [
        f"\n---\n\n## {case.get('id')} · {GROUP_NAMES.get(case.get('group'), case.get('group'))} —— {mark}\n",
        f"**用户行为**：{case.get('input')}\n",
    ]
    if not ok:
        lines.append(f"> 失败原因：{result.get('failed_reason') or result.get('error') or '未记录'}\n")
        diffs = diff_calls(row.get("expected") or [], row.get("actual") or [])
        if diffs:
            lines.append("**差异明细**\n")
            lines.extend(diffs)
            lines.append("")

    lines.append(f"**该用例可操作白名单**：{alias_table(whitelist)}\n")
    visible = whitelist.get("visible") or []
    lines.append(
        f"**运行中实际可见的 id**（本轮 `task.search` 返回并写入 guard 白名单）："
        f"{'、'.join(short(i) for i in visible) if visible else '（空 —— 本轮没有搜索到任何任务）'}\n"
    )

    lines.append("**标准答案（期望调用）**\n")
    expected = row.get("expected") or []
    if expected:
        lines.append("| # | 工具 | 参数 |")
        lines.append("|---:|---|---|")
        for i, call in enumerate(expected, 1):
            lines.append(f"| {i} | `{call.get('name')}` | `{fmt_args(call.get('args'))}` |")
        lines.append("")
    else:
        lines.append("（无工具调用，只需回答）\n")

    lines.append("**实际调用**\n")
    actual = [c for c in (row.get("actual") or []) if c.get("name") != "meta.finish"]
    if actual:
        lines.append("| # | 工具 | 参数 | 结果 | 耗时 |")
        lines.append("|---:|---|---|---|---:|")
        for i, call in enumerate(actual, 1):
            state = "OK" if call.get("ok") else "FAIL"
            lines.append(
                f"| {i} | `{call.get('name')}` | `{fmt_args(call.get('args'))}` | {state} | "
                f"{call.get('duration_ms', 0)}ms |"
            )
        lines.append("")
    else:
        lines.append("（没有工具调用）\n")

    lines.append("**逐轮过程**")
    lines.extend(steps_block(row.get("steps") or []))
    lines.append("")

    lines.append(
        f"**Loop 汇总**：{summary.get('steps', 0)} 步 ｜ 总耗时 {summary.get('latency_ms', 0)}ms"
        f"（LLM {summary.get('llm_ms', 0)}ms / 工具 {summary.get('tool_ms', 0)}ms）"
        f" ｜ tokens {summary.get('tokens_in', 0)} → {summary.get('tokens_out', 0)}"
        f" ｜ 花费 **{summary.get('cost_micro_yuan', 0)} 微元**"
        f" ｜ 结束原因 `{summary.get('ended_by') or '-'}`"
        + (" ｜ **已降级**" if summary.get("degraded") else "")
        + "\n"
    )

    answer = (row.get("final_answer") or "").strip()
    lines.append("**模型最终回答**\n")
    lines.append("\n".join(f"> {line}" for line in (answer.splitlines() or ["（空）"])))
    lines.append("")
    return lines


def main(argv: list[str]) -> int:
    if not argv:
        print("用法：python scripts/report_detailed.py <run_root> | --latest", file=sys.stderr)
        return 2
    if argv[0] == "--latest":
        runs = sorted(
            (p for p in (REPO / ".tmp" / "evals").glob("run-*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
        )
        if not runs:
            print("没有找到任何 run 目录", file=sys.stderr)
            return 1
        run_root = runs[-1]
    else:
        run_root = Path(argv[0])
    rows = load_cases(run_root)
    if not rows:
        print(f"{run_root} 里没有 case.json", file=sys.stderr)
        return 1

    total = len(rows)
    passed = sum(1 for r in rows if (r.get("result") or {}).get("passed"))
    tokens_in = sum(int((r.get("summary") or {}).get("tokens_in") or 0) for r in rows)
    tokens_out = sum(int((r.get("summary") or {}).get("tokens_out") or 0) for r in rows)
    micro = sum(int((r.get("summary") or {}).get("cost_micro_yuan") or 0) for r in rows)
    latency = sum(int((r.get("summary") or {}).get("latency_ms") or 0) for r in rows)
    mode = next((r.get("mode") for r in rows if r.get("mode")), "live+live")
    model = next((r.get("model") for r in rows if r.get("model")), "unknown")

    out: list[str] = []
    out.append("# Cadence 详细评测报告\n")
    out.append(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    out.append(f"- 运行目录：`{run_root.as_posix()}`")
    out.append(f"- 模式：`{mode}`（真实 Core + DeepSeek API；未使用 GLM）")
    out.append(f"- 模型：`{model}`")
    out.append(f"- 数据集：v1（35 条，时间基准 2026-11-08 10:30 +08:00）")
    out.append(f"- 结果：**{passed}/{total}** 通过（{passed / total * 100:.1f}%）\n")

    out.append("## 口径说明\n")
    out.append(
        "- **一轮** = 一次 LLM 调用（同一 loop 内可能有多轮）。\n"
        "- **白名单**：运行时由本轮 `task.search` 返回的 id 构成；"
        "表里的「可操作白名单」是该用例 seed 出来的全部任务，"
        "「运行中实际可见」是本轮真正进了 guard 白名单的 id。\n"
        "- **微元** = 1e-6 元；成本按整数纳元累加后换算，不做浮点累加。\n"
        "- 思考过程：`deepseek-flash` 会返回 `reasoning_content`，报告里原样展示（截断 1200 字）；"
        "某个步骤没有 reasoning，表示该步模型没有展开推理。\n"
    )

    out.append("## 总览\n")
    out.append("| 用例 | 分组 | 结果 | 步数 | 耗时 | tokens in/out | 微元 |")
    out.append("|---|---|---|---:|---:|---|---:|")
    for row in rows:
        s = row.get("summary") or {}
        r = row.get("result") or {}
        c = row.get("case") or {}
        out.append(
            f"| {c.get('id')} | {GROUP_NAMES.get(c.get('group'), c.get('group'))} | "
            f"{'✅' if r.get('passed') else '❌'} | {s.get('steps', 0)} | {s.get('latency_ms', 0)}ms | "
            f"{s.get('tokens_in', 0)}/{s.get('tokens_out', 0)} | {s.get('cost_micro_yuan', 0)} |"
        )
    out.append("")
    out.append(
        f"**合计**：{total} 条 ｜ 总耗时 {latency / 1000:.1f}s ｜ "
        f"tokens {tokens_in} in / {tokens_out} out ｜ 总花费 **{micro} 微元**（¥{micro / 1_000_000:.4f}）\n"
    )

    for i, row in enumerate(rows, 1):
        out.extend(case_section(row, i))

    slug = model.replace(".", "").replace("/", "-")
    path = REPO / "evals" / "reports" / f"{datetime.now():%Y-%m-%d}-详细-{slug}.md"
    # 绝不覆盖已有报告：同名（同一天、同模型）但 run 不同时，加上 run 目录后缀。
    # 详细报告是"历史对比"的证据，覆盖一次就丢一次基线。
    if path.exists():
        path = path.with_name(f"{path.stem}-{run_root.name}.md")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {path}  ({len(out)} lines, {passed}/{total} passed, {micro} micro-yuan)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
