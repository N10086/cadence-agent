"""命令行工具。

    python -m cadence.cli trace <trace_id>     # 打印一条 trace 的时间线
    python -m cadence.cli list                  # 列出最近的 trace
    python -m cadence.cli schema                # 打印发给模型的工具 schema

面试时直接翻给人看：一条 trace 就能说明"每步花了多少、哪一步失败了"。
"""

from __future__ import annotations

import sys

from .config import get_settings
from .runtime.trace import find_trace_file, format_timeline, load_trace


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    settings = get_settings()
    trace_dir = settings.resolved_trace_dir

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    command = args[0]

    if command == "trace":
        if len(args) < 2:
            print("用法：python -m cadence.cli trace <trace_id>")
            return 2
        data = load_trace(trace_dir, args[1])
        if data is None:
            print(f"找不到 trace：{args[1]}（目录 {trace_dir}）")
            return 1
        print(format_timeline(data))
        return 0

    if command == "list":
        rows = sorted(trace_dir.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not rows:
            print(f"还没有 trace（目录 {trace_dir}）")
            return 0
        for path in rows[:20]:
            print(f"{path.stem:<24} {path.stat().st_size:>8}B  {path.parent.name}")
        return 0

    if command == "schema":
        import json

        from .tools.registry import build_default_registry

        registry = build_default_registry()
        print(json.dumps(registry.openai_tools(), ensure_ascii=False, indent=2))
        print(
            f"\n共 {len(registry)} 个工具，暴露给模型 {len(registry.exposed())} 个，"
            f"schema 约 {registry.schema_token_estimate()} token",
            file=sys.stderr,
        )
        return 0

    print(f"未知命令：{command}")
    print(__doc__)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
