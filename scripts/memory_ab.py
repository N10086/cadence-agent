"""长对话 A/B：无记忆 / 固定窗口 / 三级记忆。

指标：每轮 token in/out、耗时、费用；3 个回忆问题的关键词命中率。
真实模型（DeepSeek），内存版 Core（FakeCore），不碰数据库。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "services" / "cadence-agent" / "src"))
sys.path.insert(0, str(REPO / "services" / "cadence-agent" / "tests"))

from cadence.config import Settings  # noqa: E402
from cadence.deps import Runtime, build_llm  # noqa: E402
from cadence.guard.confirm import ConfirmStore  # noqa: E402
from cadence.promptlib import system_prompt  # noqa: E402
from cadence.runtime.budget import Budget  # noqa: E402
from cadence.runtime.context import TurnContext  # noqa: E402
from cadence.runtime.memory import MemoryConfig, compact  # noqa: E402
from cadence.runtime.trace import TraceRecorder  # noqa: E402
from cadence.llm.router import route  # noqa: E402
from fixtures.fake_core import FakeCore, _task  # noqa: E402

TURNS = [
    "记住一个信息，不要创建任务：我的工位在 3 楼东侧，门禁密码是 0427。",
    "我现在有哪些任务？",
    "把买牛奶标记完成。",
    "高优先级、还没开始的有哪些？",
    "我的工位在哪一层？门禁密码是多少？",              # recall 1（只可能来自记忆）
    "把写周报的优先级改成紧急。",
    "我刚才让你完成的是哪个任务？",                      # recall 2（对话历史）
    "再说一遍我的工位和门禁密码？",                      # recall 3（只可能来自记忆）
]

#: turn index -> 需要命中的关键词组（任一同义词命中即算对）
RECALL: dict[int, list[list[str]]] = {
    4: [["3 楼", "3楼", "三楼"], ["0427"]],
    6: [["买牛奶"]],
    7: [["3 楼", "3楼", "三楼"], ["0427"]],
}

ARMS = {
    "A 无记忆": dict(
        memory_enabled=False,
        context_recent_turns=0,
    ),
    "B 固定窗口 3 轮": dict(
        memory_enabled=False,
        context_recent_turns=3,
    ),
    "C 三级记忆": dict(
        memory_enabled=True,
        context_recent_turns=8,
        memory_window_tokens=800,
        memory_max_turns=8,
    ),
}


def build_core() -> FakeCore:
    return FakeCore(
        [
            _task("t-milk", "买牛奶", priority="NORMAL", tags=["生活"]),
            _task("t-report", "写周报", priority="HIGH", dueAt="2026-11-13T18:00:00+08:00"),
            _task("t-demo", "客户演示", priority="URGENT", startAt="2026-11-11T14:00:00+08:00"),
            _task("t-old", "旧任务", status="CANCELLED", priority="LOW"),
        ]
    )


async def noop(_event: str, _payload: dict) -> None:
    return None


def score(turn_index: int, text: str) -> float | None:
    groups = RECALL.get(turn_index)
    if not groups:
        return None
    answer = text or ""
    hits = sum(1 for alts in groups if any(a in answer for a in alts))
    return hits / len(groups)


async def run_arm(name: str, overrides: dict, llm) -> dict:
    settings = Settings()
    for key, value in overrides.items():
        setattr(settings, key, value)
    settings.ensure_dirs()

    core = build_core()
    runtime = Runtime(settings, llm=llm, core=core)
    # 每次运行用独立会话 id，避免读到上一次落盘的 session（会污染 A/B）
    run_id = int(time.time() * 1000)
    session = runtime.sessions.get(f"ab-{run_id}-{name}")

    records = []
    for index, message in enumerate(TURNS):
        tier = route(message).tier.value
        trace_id = f"ab-{run_id}-{name}-{index}"
        trace = TraceRecorder(
            trace_id,
            settings.resolved_trace_dir,
            session_id=session.session_id,
            prompt_version=system_prompt(tier)[1],
        )
        ctx = TurnContext(
            settings=settings,
            core=runtime.core,
            trace=trace,
            confirms=ConfirmStore(settings.confirm_ttl_s),
            trace_id=trace_id,
            session_id=session.session_id,
            budget=Budget(max_steps=settings.step_budget, max_tokens=settings.token_budget_per_turn),
        )

        started = time.perf_counter()
        outcome = await runtime.loop.run_turn(
            ctx, user_message=message, session=session, emit=noop
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        keep = max(settings.memory_max_turns, settings.session_recent_turns)
        session.add_turn("user", message, keep=keep)
        if outcome.text:
            session.add_turn("assistant", outcome.text, keep=keep)
        if settings.memory_enabled:
            compact(
                session,
                MemoryConfig(
                    window_tokens=settings.memory_window_tokens,
                    max_turns=settings.memory_max_turns,
                    summary_chars=settings.memory_summary_chars,
                    facts_limit=settings.memory_facts_limit,
                ),
            )
        trace.close(cost_yuan=outcome.cost_yuan, total_ms=elapsed_ms, degraded=outcome.degraded)

        records.append(
            {
                "index": index + 1,
                "message": message,
                "answer": (outcome.text or "")[:300],
                "tokens_in": ctx.budget.tokens_in,
                "tokens_out": ctx.budget.tokens_out,
                "latency_ms": elapsed_ms,
                "cost_micro": round(outcome.cost_yuan * 1_000_000),
                "steps": outcome.steps,
                "score": score(index, outcome.text or ""),
                "summary_chars": len(session.summary),
                "facts": len(session.facts),
            }
        )

    await runtime.aclose()

    scored = [r["score"] for r in records if r["score"] is not None]
    return {
        "arm": name,
        "records": records,
        "total_tokens_in": sum(r["tokens_in"] for r in records),
        "total_tokens_out": sum(r["tokens_out"] for r in records),
        "total_cost_micro": sum(r["cost_micro"] for r in records),
        "avg_latency_ms": round(sum(r["latency_ms"] for r in records) / len(records)),
        "accuracy": round(sum(scored) / len(scored), 3) if scored else 0.0,
    }


async def main() -> int:
    settings = Settings()
    llm = build_llm(settings)  # live；共享一份 client

    results = []
    for name, overrides in ARMS.items():
        print(f"running {name} ...", flush=True)
        results.append(await run_arm(name, overrides, llm))

    out = Path(".tmp/memory_ab.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 汇总 ===")
    print(f"{'arm':<16}{'in':>7}{'out':>7}{'cost¥':>9}{'avg ms':>8}{'acc':>7}")
    for r in results:
        print(
            f"{r['arm']:<16}{r['total_tokens_in']:>7}{r['total_tokens_out']:>7}"
            f"{r['total_cost_micro'] / 1e6:>9.4f}{r['avg_latency_ms']:>8}{r['accuracy']:>7.2f}"
        )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
