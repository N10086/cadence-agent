"""L3-5 并发压测：50 会话 × 20 轮，5 个场景 + 2 个背压探针。

**它验证的不是"QPS 有多高"**（单机 fake 没有意义），而是四条不变量：

| 不变量 | 怎么断言 |
|---|---|
| 不死锁 | 每轮都有上限（60s）；超时即 `deadlock>0`，脚本 fail |
| 不串会话 | 同会话 20 并发后，`recent_turns` 里 20 条用户消息一条不少、内容不串 |
| 不泄漏锁 | 场景结束 `sessions.locked_count() == 0` |
| 超载快速失败 | 准入队列满时返回 503（比例打印，不当作失败） |

输出里固定出现：`lock_leak=0`、`deadlock=0`、`queue_wait_ms p50=… p95=…`、
`503=… (…%)`、`409=… (…%)` —— L4 的 `T_queue` / `overload_503_total` 直接取这些数。

用法：

    python scripts/load.py --mode fake --sessions 50 --rounds 20 --assert-no-leak
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

REPO = Path(__file__).resolve().parents[1]
AGENT = REPO / "services" / "cadence-agent"
for _p in (AGENT / "src", AGENT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cadence.api.chat import start_turn  # noqa: E402
from cadence.config import Settings  # noqa: E402
from cadence.deps import Runtime  # noqa: E402
from cadence.llm.base import LLMEvent, LLMResponse, ToolCall  # noqa: E402
from cadence.runtime.errors import ErrorCode, ToolError  # noqa: E402

from fixtures.fake_core import FakeCore  # noqa: E402

TURN_DEADLINE_S = 60.0
LONG_RUN_SECONDS = 1.5


# ---------------------------------------------------------------------------
# 确定性 fake client：无共享游标，天然并发安全
# ---------------------------------------------------------------------------


class ScriptedClient:
    """按"上下文里有没有 tool 消息"决定下一步：第一跳调工具，第二跳汇报。

    不用 `FakeLLM` 的脚本游标：1000 个并发 turn 共享一个游标会互相消费对方的剧本，
    压测就变成测竞态了。这里每个 turn 的状态全部来自自己的 `messages`。
    """

    name = "load-fake"

    def __init__(self, *, mode: str = "read", delay_s: float = 0.0) -> None:
        self.mode = mode
        self.delay_s = delay_s

    async def chat(self, *a: Any, **k: Any) -> LLMResponse:  # pragma: no cover - 不走
        raise AssertionError("压测只走 stream")

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **kw: Any
    ) -> AsyncIterator[LLMEvent]:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        seen_tool = any(m.get("role") == "tool" for m in messages)
        if not seen_tool:
            name = "task.search" if self.mode == "read" else "task.create"
            args = {"limit": 5} if self.mode == "read" else {"title": f"load-{uuid.uuid4().hex[:8]}"}
            yield LLMEvent(
                kind="done",
                response=LLMResponse(
                    tool_calls=[ToolCall(name=name, arguments=args)],
                    model="load-fake",
                ),
            )
            return
        yield LLMEvent(
            kind="done",
            response=LLMResponse(text="做好了", tokens_in=10, tokens_out=5, model="load-fake"),
        )


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


@dataclass
class ScenarioStats:
    name: str
    turns: int = 0
    ok: int = 0
    overload_503: int = 0
    busy_409: int = 0
    other_failed: int = 0
    deadlocks: int = 0
    elapsed_s: float = 0.0
    max_queue_inflight: int = 0
    lock_leak: int = 0
    queue_waits: list[float] = field(default_factory=list)
    errors: Counter = field(default_factory=Counter)


def _snapshot(rt: Runtime, stats: ScenarioStats) -> None:
    """场景收尾时把队列/锁状态抄进 stats —— 必须在 aclose 之前调。"""
    stats.lock_leak = rt.sessions.locked_count()
    stats.queue_waits = rt.queue.wait_samples()


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    k = (len(ordered) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    value = ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)
    return round(value, 3)


def _new_runtime(
    root: Path,
    *,
    queue_capacity: int = 64,
    mode: str = "read",
    core_delay_s: float = 0.0,
    llm_delay_s: float = 0.0,
    session_lock_timeout_s: float = 20.0,
) -> Runtime:
    settings = Settings(
        llm_mode="fake",
        deepseek_api_key="",
        data_dir=str(root),
        llm_retry_times=0,
        llm_retry_backoff_s=0.0,
        core_retry_times=0,
        core_retry_backoff_s=0.0,
        # 压测要看"有没有覆盖"，所以历史不能只留 3 轮
        session_recent_turns=1000,
        session_lock_timeout_s=session_lock_timeout_s,
        turn_queue_capacity=queue_capacity,
        llm_max_concurrency=16,
        turn_timeout_s=TURN_DEADLINE_S,
    )
    core = FakeCore(
        [
            {"id": "t_001", "title": "买牛奶", "status": "TODO", "priority": "NORMAL", "version": 0},
            {"id": "t_002", "title": "写周报", "status": "TODO", "priority": "HIGH", "version": 1},
        ]
    )
    original_request = core.request

    async def delayed(method: str, path: str, **kw: Any) -> Any:
        if core_delay_s:
            await asyncio.sleep(core_delay_s)
        return await original_request(method, path, **kw)

    core.request = delayed  # type: ignore[assignment]
    return Runtime(settings, llm=ScriptedClient(mode=mode, delay_s=llm_delay_s), core=core)


async def _run_turn(rt: Runtime, stats: ScenarioStats, session_id: str, message: str) -> None:
    stats.turns += 1
    try:
        handle = await start_turn(rt, session_id, message)
    except ToolError as exc:
        stats.errors[exc.code.value] += 1
        if exc.code is ErrorCode.OVERLOADED:
            stats.overload_503 += 1
        else:
            stats.other_failed += 1
        return

    stats.max_queue_inflight = max(stats.max_queue_inflight, rt.queue.inflight)
    try:
        await asyncio.wait_for(handle.task, timeout=TURN_DEADLINE_S)
    except TimeoutError:
        stats.deadlocks += 1
        return

    buf = rt.streams.get(handle.stream_id)
    events = buf.events if buf is not None else []
    done = next((e.data for e in events if e.event == "done"), None)
    if done is None:
        stats.other_failed += 1
        stats.errors["NO_DONE"] += 1
        return
    if done.get("endedBy") == "busy" or done.get("errorCode") == ErrorCode.SESSION_BUSY.value:
        stats.busy_409 += 1
    elif done.get("endedBy") == "finish":
        stats.ok += 1
    else:
        stats.other_failed += 1
        stats.errors[str(done.get("errorCode") or done.get("endedBy"))] += 1


# ---------------------------------------------------------------------------
# 五个场景
# ---------------------------------------------------------------------------


async def scenario_same_session(root: Path, sessions: int, rounds: int) -> ScenarioStats:
    """1 个会话 × N 条并发：全部排队 → 串行执行 → 会话历史一条不少。"""
    stats = ScenarioStats("same-session")
    rt = _new_runtime(root / "same")
    try:
        sid = "s_same"
        messages = [f"第{i}轮：看看任务" for i in range(rounds)]
        started = time.perf_counter()
        await asyncio.gather(*[_run_turn(rt, stats, sid, m) for m in messages])
        stats.elapsed_s = time.perf_counter() - started

        session = rt.sessions.get(sid)
        users = [t.content for t in session.recent_turns if t.role == "user"]
        assert stats.ok == rounds, f"同会话 {rounds} 并发应全部成功，实际 {stats.ok}"
        assert len(users) == rounds, f"会话历史被覆盖：期望 {rounds} 条，实际 {len(users)}"
        assert sorted(users) == sorted(messages), "会话里的用户消息内容对不上（串台）"
        assert rt.sessions.locked_count() == 0, "锁泄漏"
    finally:
        _snapshot(rt, stats)
        await rt.aclose()
    return stats


async def scenario_cross_session(root: Path, sessions: int, rounds: int) -> ScenarioStats:
    """50 个会话各跑 20 轮（会话内串行、会话间并发）。"""
    stats = ScenarioStats("cross-session")
    rt = _new_runtime(root / "cross")
    try:
        async def session_worker(n: int) -> None:
            sid = f"s_{n:02d}"
            for r in range(rounds):
                await _run_turn(rt, stats, sid, f"会话{n} 第{r}轮")

        started = time.perf_counter()
        await asyncio.gather(*[session_worker(n) for n in range(sessions)])
        stats.elapsed_s = time.perf_counter() - started

        expected = sessions * rounds
        assert stats.ok == expected, f"期望 {expected} 成功，实际 {stats.ok}（{stats.errors}）"
        for n in range(sessions):
            users = [t for t in rt.sessions.get(f"s_{n:02d}").recent_turns if t.role == "user"]
            assert len(users) == rounds, f"会话 s_{n:02d} 覆盖：{len(users)} != {rounds}"
        assert rt.sessions.locked_count() == 0, "锁泄漏"
    finally:
        _snapshot(rt, stats)
        await rt.aclose()
    return stats


async def scenario_mixed(root: Path, sessions: int, rounds: int) -> ScenarioStats:
    """一半会话读、一半会话写：读写混跑时锁与信号量不互相拖死。"""
    stats = ScenarioStats("mixed-rw")
    rt = _new_runtime(root / "mixed")
    try:
        half = max(1, sessions // 2)

        async def writer(n: int) -> None:
            for r in range(rounds):
                await _run_turn(rt, stats, f"w_{n:02d}", f"新建任务 {n}-{r}")

        async def reader(n: int) -> None:
            for r in range(rounds):
                await _run_turn(rt, stats, f"r_{n:02d}", f"看看任务 {n}-{r}")

        started = time.perf_counter()
        await asyncio.gather(
            *[writer(n) for n in range(half)],
            *[reader(n) for n in range(half)],
        )
        stats.elapsed_s = time.perf_counter() - started

        assert stats.ok == half * 2 * rounds, f"混合读写有失败：{stats.ok}/{half * 2 * rounds} {stats.errors}"
        assert rt.sessions.locked_count() == 0, "锁泄漏"
    finally:
        _snapshot(rt, stats)
        await rt.aclose()
    return stats


async def scenario_core_slow(root: Path, sessions: int, rounds: int) -> ScenarioStats:
    """Core 每次慢 30ms：验证慢下游不会把并发压成串行、也不会拖过 60s。"""
    stats = ScenarioStats("core-slow")
    rt = _new_runtime(root / "slow", core_delay_s=0.03)
    try:
        workers = min(sessions, 20)

        async def worker(n: int) -> None:
            for r in range(rounds):
                await _run_turn(rt, stats, f"c_{n:02d}", f"会话{n} 第{r}轮")

        started = time.perf_counter()
        await asyncio.gather(*[worker(n) for n in range(workers)])
        stats.elapsed_s = time.perf_counter() - started

        assert stats.ok == workers * rounds
        assert stats.elapsed_s < TURN_DEADLINE_S
        assert rt.sessions.locked_count() == 0
    finally:
        _snapshot(rt, stats)
        await rt.aclose()
    return stats


async def scenario_long_run(root: Path, sessions: int, rounds: int) -> ScenarioStats:
    """长稳：连续 churn N 秒，结束后 inflight=0、锁=0（没有半途泄漏的槽位）。"""
    stats = ScenarioStats("long-run")
    rt = _new_runtime(root / "long", core_delay_s=0.005)
    try:
        deadline = time.perf_counter() + LONG_RUN_SECONDS
        workers = min(sessions, 10)

        async def worker(n: int) -> None:
            r = 0
            while time.perf_counter() < deadline:
                await _run_turn(rt, stats, f"l_{n:02d}", f"长稳 {n}-{r}")
                r += 1

        started = time.perf_counter()
        await asyncio.gather(*[worker(n) for n in range(workers)])
        stats.elapsed_s = time.perf_counter() - started

        assert stats.other_failed == 0 and stats.deadlocks == 0, f"长稳出现失败：{stats.errors}"
        assert rt.queue.inflight == 0, f"长稳结束仍有 inflight={rt.queue.inflight}"
        assert rt.sessions.locked_count() == 0, "锁泄漏"
    finally:
        _snapshot(rt, stats)
        await rt.aclose()
    return stats


# ---------------------------------------------------------------------------
# 两个背压探针（不是"场景"，是证明 503 / 409 真的会发生）
# ---------------------------------------------------------------------------


async def probe_overload(root: Path) -> ScenarioStats:
    """队列容量 4 + Core 慢：12 个并发里应有 8 个被 503 挡下。"""
    stats = ScenarioStats("overload-probe")
    rt = _new_runtime(root / "probe-overload", queue_capacity=4, core_delay_s=0.05)
    try:
        await asyncio.gather(*[_run_turn(rt, stats, f"o_{i}", "看看任务") for i in range(12)])
    finally:
        _snapshot(rt, stats)
        await rt.aclose()
    return stats


async def probe_session_busy(root: Path) -> ScenarioStats:
    """同一会话、锁等待 0.05s：第二条应拿到 SESSION_BUSY(409)。"""
    stats = ScenarioStats("session-busy-probe")
    rt = _new_runtime(root / "probe-busy", llm_delay_s=0.4, session_lock_timeout_s=0.05)
    try:
        await asyncio.gather(
            _run_turn(rt, stats, "s_busy", "第一条"),
            _run_turn(rt, stats, "s_busy", "第二条"),
        )
    finally:
        _snapshot(rt, stats)
        await rt.aclose()
    return stats


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    run_root = REPO / ".tmp" / "load" / f"run-{uuid.uuid4().hex[:8]}"
    run_root.mkdir(parents=True, exist_ok=True)

    scenarios = [
        ("same-session", lambda: scenario_same_session(run_root, args.sessions, args.rounds)),
        ("cross-session", lambda: scenario_cross_session(run_root, args.sessions, args.rounds)),
        ("mixed-rw", lambda: scenario_mixed(run_root, args.sessions, args.rounds)),
        ("core-slow", lambda: scenario_core_slow(run_root, args.sessions, args.rounds)),
        ("long-run", lambda: scenario_long_run(run_root, args.sessions, args.rounds)),
    ]

    failures: list[str] = []
    collected: list[ScenarioStats] = []

    for index, (name, run) in enumerate(scenarios, start=1):
        try:
            stats = await run()
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
            print(f"FAIL {name} {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - 压测要把任何炸法变成 FAIL 行
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"FAIL {name} {type(exc).__name__}: {exc}")
            continue
        collected.append(stats)
        print(
            f"[{index}/5] {name:<14} turns={stats.turns:<5} ok={stats.ok:<5} "
            f"503={stats.overload_503} 409={stats.busy_409} "
            f"deadlock={stats.deadlocks} elapsed={stats.elapsed_s:.2f}s "
            f"queue_max={stats.max_queue_inflight}"
        )
        print(f"PASS {name}")

    # 两个探针：证明 503 / 409 真的会发生（不是只写在文档里）
    overload = await probe_overload(run_root)
    busy = await probe_session_busy(run_root)
    collected.extend([overload, busy])
    print(f"[probe] overload    turns={overload.turns} 503={overload.overload_503}")
    print(f"[probe] session-busy 409={busy.busy_409}")
    if overload.overload_503 == 0:
        failures.append("overload-probe: 队列满了却没有 503")
    if busy.busy_409 == 0:
        failures.append("session-busy-probe: 等锁超时却没有 409")
    print(f"PASS overload-probe 503={overload.overload_503}")
    print(f"PASS session-busy-probe 409={busy.busy_409}")

    # ---- 汇总 ----
    turns = sum(s.turns for s in collected)
    ok = sum(s.ok for s in collected)
    total_503 = sum(s.overload_503 for s in collected)
    total_409 = sum(s.busy_409 for s in collected)
    deadlocks = sum(s.deadlocks for s in collected)
    lock_leak = sum(s.lock_leak for s in collected)
    waits = [w for s in collected for w in s.queue_waits]
    max_inflight = max((s.max_queue_inflight for s in collected), default=0)

    print(f"queue_wait_ms p50={_percentile(waits, 50):.3f} p95={_percentile(waits, 95):.3f} "
          f"samples={len(waits)} max_inflight={max_inflight}")
    print(f"turns={turns} ok={ok} 503={total_503} ({total_503 / max(1, turns):.1%}) "
          f"409={total_409} ({total_409 / max(1, turns):.1%})")
    print(f"lock_leak={lock_leak} deadlock={deadlocks}")

    if lock_leak != 0:
        failures.append(f"lock_leak={lock_leak}")
    if deadlocks != 0:
        failures.append(f"deadlock={deadlocks}")
    if args.assert_no_leak and lock_leak != 0:
        failures.append("--assert-no-leak 失败")

    if failures:
        for item in failures:
            print(f"FAIL {item}")
        return 1

    print("PASS load")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cadence L3 并发压测")
    parser.add_argument("--mode", choices=["fake"], default="fake")
    parser.add_argument("--sessions", type=int, default=50)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--assert-no-leak", action="store_true")
    return asyncio.run(main_async(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
