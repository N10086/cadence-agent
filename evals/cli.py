"""评测 CLI（L4-5）。

用法（与构建方案 §8.1 对齐）：

    python -m evals.cli run --all --llm fake --core fake --out evals/reports/
    python -m evals.cli run --case B2 --llm live --core live
    python -m evals.cli run --all --llm fake --core fake --repeat 3

未知 `--case` 返回非 0 并打印候选 id —— 不为了迁就冻结测试而假装成功。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from .bootstrap import REPO_ROOT
from .dataset import Dataset, load_dataset
from .metrics import compute_metrics
from .report import build_report, render_markdown
from .runner import Runner, RunSummary

DEFAULT_DATASET = REPO_ROOT / "evals" / "datasets" / "v1"
DEFAULT_OUT = REPO_ROOT / "evals" / "reports"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.cli",
        description="Cadence 工具调用评测（35 条 · 13 指标 · 六表报告）",
    )
    parser.add_argument("--all", action="store_true", help="跑全部用例")
    parser.add_argument("--case", action="append", default=[], metavar="ID", help="只跑指定用例（可重复）")
    parser.add_argument("--repeat", type=int, default=1, help="重复轮数（默认 1；>1 时禁用缓存）")
    parser.add_argument("--llm", choices=["fake", "live"], default="fake", help="LLM 模式")
    parser.add_argument("--core", choices=["fake", "live"], default="fake", help="Core 模式")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="数据集目录或文件")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="报告输出目录")
    parser.add_argument("--agent-url", default="http://127.0.0.1:18000", help="外部 Agent 地址（live+live）")
    parser.add_argument("--core-url", default="http://127.0.0.1:18080", help="Core 地址")
    parser.add_argument("--no-cache", action="store_true", help="禁用结果缓存")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "run":
        raw = raw[1:]
    args = _parser().parse_args(raw)

    if args.all and args.case:
        print("--all 与 --case 不能同时使用", file=sys.stderr)
        return 2
    if args.repeat < 1:
        print("--repeat 必须 >= 1", file=sys.stderr)
        return 2

    try:
        dataset = load_dataset(args.dataset)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"加载数据集失败：{exc}", file=sys.stderr)
        return 1

    selected = _select_cases(dataset, args.case)
    if selected is None:
        return 2

    runner = Runner(
        dataset,
        llm=args.llm,
        core=args.core,
        agent_url=args.agent_url,
        core_url=args.core_url,
        cache=False if args.no_cache else None,
        repeat=args.repeat,
    )
    try:
        summary = asyncio.run(runner.run(selected))
    except Exception as exc:  # noqa: BLE001 - CLI 要把"跑不起来"变成可读的一行
        print(f"评测失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    report = build_report(summary, generated_at=datetime.now().isoformat(timespec="seconds"))
    if summary.repeat > 1:
        repeat_metrics = [compute_metrics(rows) for rows in summary.repeat_rows]
        report["repeat_consistency"] = all(item == repeat_metrics[0] for item in repeat_metrics)

    markdown = render_markdown(report)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = summary.mode.replace("+", "-")
    out_path = out_dir / f"{datetime.now():%Y-%m-%d}-{suffix}.md"
    out_path.write_text(markdown, encoding="utf-8")

    metrics = report["metrics"]
    print(f"报告：{out_path}")
    print(f"用例：{len(summary.rows)}  通过：{report['strict_passed']}/{len(summary.rows)}")
    print("13 指标：" + json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    if summary.repeat > 1:
        print(
            f"repeat={summary.repeat} 全部指标逐字节一致：{report.get('repeat_consistency')}"
            "（含 latency_p50/p95；时延随机器负载波动，稳定复算请看缓存命中或中位数）"
        )
    return 0


def _select_cases(dataset: Dataset, wanted: list[str]) -> list | None:
    if not wanted:
        return dataset.cases
    selected = []
    for case_id in wanted:
        case = dataset.by_id(case_id)
        if case is None:
            candidates = ", ".join(case.id for case in dataset.cases)
            print(f"未知用例：{case_id}", file=sys.stderr)
            print(f"候选：{candidates}", file=sys.stderr)
            return None
        selected.append(case)
    return selected


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
