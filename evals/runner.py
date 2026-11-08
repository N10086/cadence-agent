"""评测 Runner（L4-2）。

一条用例的流水线（顺序固定）：

    reset（Core 清表，走 API）
      → seed（建 8 条背景任务，拿到 别名→真实 id / version）
      → POST /chat（202）
      → tail GET /stream/{id} 到 done
      → GET /trace/{id}
      → Core 快照
      → 三类断言（tool_calls / final_state / must_not_call）
      → CaseResult

两种运行方式：

| 模式 | Agent | Core | 用途 |
|---|---|---|---|
| `--llm fake --core fake` | 进程内 ASGI（真实 FastAPI 路由） | FakeCore | 流水线验证 |
| `--llm live --core live` | 外部已启动的 Agent | 外部真实 Core | 真实数字 |

`--core fake` 时**复用** `tests/fixtures/fake_core.py`；不另写一套。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from cadence.config import Settings, get_settings
from cadence.deps import Runtime, build_llm, set_runtime
from cadence.llm.fake import FakeLLM, call, say
from cadence.main import create_app
from cadence.tools.core_client import CoreClient
from fixtures.fake_core import FakeCore

from . import metrics
from .bootstrap import REPO_ROOT
from .cache import RunCache
from .dataset import Case, Dataset, load_dataset, resolve_case
from .sandbox import CoreSandbox, ResetNotAllowed

#: 题面里的"今天/本周三/本周五"都相对这个时间。注入到 in-process Loop，
#: 保证 fake 与 live 的日期语义可复现（live 外部 Agent 需自行保证时钟一致）。
EVAL_NOW = datetime(2026, 11, 8, 10, 30, tzinfo=timezone(timedelta(hours=8)))

_GUARD_BLOCK_ROWS = {
    "guard.args",
    "guard.whitelist",
    "guard.confirm_required",
    "guard.confirm_invalid",
    "guard.confirm_stale",
    "guard.fingerprint",
}
_GUARD_BLOCK_CODES = {
    "CONFIRM_REQUIRED",
    "TASK_ID_NOT_IN_CONTEXT",
    "VALIDATION_ERROR",
    "PLAN_STALE",
}


@dataclass
class CaseResult:
    """冻结 spec 的四个断言字段 + 其余诊断字段（都有默认值）。"""

    case_id: str = ""
    group: str = ""
    passed: bool = False
    tool_calls_ok: bool = False
    final_state_ok: bool = False
    must_not_call_ok: bool = False

    tool_calls_expected: list[dict[str, Any]] = field(default_factory=list)
    tool_calls_actual: list[dict[str, Any]] = field(default_factory=list)
    final_state: dict[str, Any] = field(default_factory=dict)
    core_task_count: int = 0

    latency_ms: int = 0
    latency_excluded: bool = False
    cost_yuan: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    degraded: bool = False
    ended_by: str = ""
    trace_id: str = ""
    prompt_version: str = ""
    model: str = ""
    tags: list[str] = field(default_factory=list)

    tool_steps: list[dict[str, Any]] = field(default_factory=list)
    llm_ms: int = 0
    tool_ms: int = 0
    queue_wait_ms: float = 0.0
    steps: int = 0

    guard_blocked: bool = False
    hallucinated_id: bool = False
    safety_attempted: int = 0
    cache_hit: bool = False
    error: str = ""

    @property
    def failed_reason(self) -> str:
        reasons: list[str] = []
        if not self.tool_calls_ok:
            reasons.append("工具调用不一致")
        if not self.final_state_ok:
            reasons.append("终态不一致")
        if not self.must_not_call_ok:
            reasons.append("命中 must_not_call")
        if self.latency_excluded:
            reasons.append(f"endedBy={self.ended_by}")
        if self.error:
            reasons.append(self.error)
        return "；".join(reasons) or "通过"


@dataclass
class RunSummary:
    rows: list[CaseResult] = field(default_factory=list)
    elapsed_s: float = 0.0
    cost_yuan: float = 0.0
    dataset_version: str = ""
    llm_mode: str = "fake"
    core_mode: str = "fake"
    cache_hit_rate: float = 0.0
    repeat: int = 1
    repeat_rows: list[list[CaseResult]] = field(default_factory=list)

    @property
    def mode(self) -> str:
        if self.llm_mode == "live" and self.core_mode == "live":
            return "live"
        if self.llm_mode == "fake" and self.core_mode == "fake":
            return "fake"
        return f"{self.llm_mode}+{self.core_mode}"


# ---------------------------------------------------------------------------


class Runner:
    def __init__(
        self,
        dataset: Dataset,
        *,
        llm: str = "fake",
        core: str = "fake",
        agent_url: str = "http://127.0.0.1:18000",
        core_url: str = "http://127.0.0.1:18080",
        cache: RunCache | bool | None = None,
        repeat: int = 1,
        run_root: str | Path | None = None,
        model: str | None = None,
        allow_reset: bool = False,
        snapshot_dir: str | Path | None = None,
    ) -> None:
        self.dataset = dataset
        self.llm_mode = llm
        self.core_mode = core
        self.allow_reset = allow_reset
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else None
        self.agent_url = agent_url.rstrip("/")
        self.core_url = core_url.rstrip("/")
        self.repeat = max(1, int(repeat))
        self.run_root = Path(run_root) if run_root else (
            REPO_ROOT / ".tmp" / "evals" / f"run-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        )
        self.run_root.mkdir(parents=True, exist_ok=True)

        if cache is False:
            self.cache: RunCache | None = None
        elif cache is None:
            self.cache = RunCache()
        elif isinstance(cache, RunCache):
            self.cache = cache
        else:
            self.cache = RunCache(cache)
        # repeat>1 时缓存会让"重复 3 轮"变成复制同一行，失去测波动的意义。
        if self.repeat > 1:
            self.cache = None

        if model:
            self.model = model
        elif llm == "fake":
            self.model = "fake-model"
        else:
            self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def run(self, cases: list[Case] | None = None) -> RunSummary:
        selected = list(cases if cases is not None else self.dataset.cases)
        # 预检放到循环之前：reset 是"删光目标库"的操作，一条 --core live 就可能清空演示数据。
        # 放在单条用例里会被 `_run_case` 的 except 吞成"35 条全失败"，看不出真正原因。
        if self.core_mode == "live" and not self.allow_reset:
            raise ResetNotAllowed(
                "core=live 会先删光目标 Core 的所有任务，默认拒绝执行。\n"
                "确认目标库可以清空后加 --allow-reset；建议同时加 "
                "--snapshot-dir .tmp/eval-snapshots 先备份。"
            )
        started = time.perf_counter()
        repeat_rows: list[list[CaseResult]] = []

        for _ in range(self.repeat):
            rows: list[CaseResult] = []
            for index, case in enumerate(selected, start=1):
                row = await self._run_with_cache(case)
                rows.append(row)
                mark = "PASS" if row.passed else "FAIL"
                suffix = "cache" if row.cache_hit else f"{row.latency_ms}ms"
                print(f"[{index}/{len(selected)}] {mark} {case.id:<4} {row.group:<12} {suffix}")
            repeat_rows.append(rows)

        elapsed = time.perf_counter() - started
        rows = repeat_rows[-1] if repeat_rows else []
        return RunSummary(
            rows=rows,
            elapsed_s=elapsed,
            cost_yuan=sum(row.cost_yuan for row in rows),
            dataset_version=self.dataset.version,
            llm_mode=self.llm_mode,
            core_mode=self.core_mode,
            cache_hit_rate=self.cache.hit_rate if self.cache else 0.0,
            repeat=self.repeat,
            repeat_rows=repeat_rows,
        )

    async def _run_with_cache(self, case: Case) -> CaseResult:
        prompt_hash = hashlib.sha256(
            f"{self.dataset.version}|{case.id}|{case.input}|{self.llm_mode}|{self.core_mode}".encode("utf-8")
        ).hexdigest()[:16]
        if self.cache is not None:
            cached = self.cache.get(model=self.model, prompt_hash=prompt_hash, input=case.input)
            if isinstance(cached, dict):
                row = _result_from_dict(cached)
                row.cache_hit = True
                return row

        row = await self._run_case(case)
        if self.cache is not None:
            self.cache.put(
                model=self.model,
                prompt_hash=prompt_hash,
                input=case.input,
                output=asdict(row),
            )
        return row

    # ------------------------------------------------------------------
    # 单条用例
    # ------------------------------------------------------------------

    async def _run_case(self, case: Case) -> CaseResult:
        case_id = case.id
        case_dir = self.run_root / f"{case_id}-{uuid.uuid4().hex[:6]}"
        case_dir.mkdir(parents=True, exist_ok=True)

        core_obj: Any = FakeCore() if self.core_mode == "fake" else CoreClient(self._settings(case_dir))
        sandbox = CoreSandbox(
            core_obj,
            label=self.core_mode,
            allow_reset=self.allow_reset,
            snapshot_dir=self.snapshot_dir,
        )
        runtime: Runtime | None = None
        http: httpx.AsyncClient | None = None

        try:
            await sandbox.reset()
            seed = await sandbox.seed(case)
            pre = await sandbox.snapshot()
            resolved = resolve_case(case, seed.aliases, seed.versions)
            expected = list(resolved["expect"]["tool_calls"])

            if self.llm_mode == "fake" or self.core_mode == "fake":
                runtime, http = await self._start_in_process(case, resolved, core_obj, case_dir)
            else:
                http = httpx.AsyncClient(
                    base_url=self.agent_url,
                    timeout=httpx.Timeout(90.0),
                )

            outcome = await self._execute_turn(http, f"eval-{case_id}-{uuid.uuid4().hex[:6]}", case.input)
            if runtime is not None:
                samples = runtime.queue.wait_samples()
                if samples:
                    outcome["queue_wait_ms"] = float(samples[-1])
            post = await sandbox.snapshot()
            row = self._build_result(case, resolved, outcome, pre, post)
            return row
        except Exception as exc:  # noqa: BLE001 - 单条用例炸了不能拖垮整份报告
            return CaseResult(
                case_id=case_id,
                group=case.group,
                tags=list(case.tags),
                error=f"{type(exc).__name__}: {exc}"[:400],
            )
        finally:
            if http is not None:
                await http.aclose()
            if runtime is not None:
                await runtime.aclose()
                set_runtime(None)
            else:
                closer = getattr(core_obj, "aclose", None)
                if closer is not None:
                    try:
                        await closer()
                    except Exception:  # noqa: BLE001
                        pass

    async def _start_in_process(
        self,
        case: Case,
        resolved: dict[str, Any],
        core_obj: Any,
        case_dir: Path,
    ) -> tuple[Runtime, httpx.AsyncClient]:
        settings = self._settings(case_dir)
        llm = FakeLLM(_fake_script(case, resolved)) if self.llm_mode == "fake" else build_llm(settings)
        runtime = Runtime(settings, llm=llm, core=core_obj)
        runtime.loop.clock = lambda: EVAL_NOW
        set_runtime(runtime)
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(
            transport=transport,
            base_url="http://eval-agent",
            timeout=httpx.Timeout(90.0),
        )
        return runtime, client

    def _settings(self, case_dir: Path) -> Settings:
        # ⚠️ 这里曾经写成 `os.getenv("DEEPSEEK_API_KEY", "")`：那条路**不读 `.env`**，
        # 而且显式传参会覆盖 env_file 里的值，于是 `--llm live` 永远报
        # "没有配置 DEEPSEEK_API_KEY"（只有把 key export 到 shell 里才能跑）。
        # 正确顺序：真实环境变量 > `.env`（由 Settings 自己的 env_file 加载）。
        base = get_settings()
        return Settings(
            llm_mode="fake" if self.llm_mode == "fake" else "live",
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or base.deepseek_api_key,
            deepseek_model=os.getenv("DEEPSEEK_MODEL") or base.deepseek_model,
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL") or base.deepseek_base_url,
            core_base_url=self.core_url,
            core_retry_times=1,
            core_retry_backoff_s=0.0,
            data_dir=str(case_dir / "agent"),
            eval_frozen_now=EVAL_NOW.isoformat(),
            turn_timeout_s=60.0,
            session_lock_timeout_s=20.0,
            llm_retry_times=0,
        )

    # ------------------------------------------------------------------
    # HTTP / SSE / trace
    # ------------------------------------------------------------------

    async def _execute_turn(
        self, client: httpx.AsyncClient, session_id: str, message: str
    ) -> dict[str, Any]:
        started = time.perf_counter()
        response = await client.post("/chat", json={"sessionId": session_id, "message": message})
        if response.status_code not in (200, 202):
            raise RuntimeError(f"POST /chat 返回 {response.status_code}: {response.text[:300]}")
        payload = response.json()
        data = payload.get("data") or payload
        stream_id = data.get("streamId")
        trace_id = data.get("traceId")
        if not stream_id or not trace_id:
            raise RuntimeError(f"POST /chat 缺少 streamId/traceId: {payload}")

        events = await _read_sse(client, stream_id)
        latency_ms = int((time.perf_counter() - started) * 1000)
        done = next((e["data"] for e in events if e["event"] == "done"), None)
        if done is None:
            raise RuntimeError(f"SSE 在 done 之前结束：{events[-1] if events else '无事件'}")

        trace = await _fetch_trace(client, trace_id)
        return {
            "events": events,
            "done": done,
            "trace": trace,
            "trace_id": trace_id,
            "latency_ms": latency_ms,
        }

    # ------------------------------------------------------------------
    # CaseResult
    # ------------------------------------------------------------------

    def _build_result(
        self,
        case: Case,
        resolved: dict[str, Any],
        outcome: dict[str, Any],
        pre: list[dict[str, Any]],
        post: list[dict[str, Any]],
    ) -> CaseResult:
        expect = resolved["expect"]
        done = outcome["done"]
        trace = outcome["trace"] or {}
        steps = list(trace.get("steps") or [])

        attempts = [
            {
                "name": event["data"].get("name", ""),
                "args": event["data"].get("args") or {},
            }
            for event in outcome["events"]
            if event.get("event") == "tool_call"
        ]
        actual = _enrich_attempts(attempts, steps)
        # CLASSIFY 阶段的歧义会在调用模型之前直接收尾，不会有 tool_call 事件。
        # 语义上它等价于一次成功的 meta.clarify，评测按工具调用统计时必须补上这一格，
        # 否则"正确地问了"会被误判成"什么都没做"。
        if str(done.get("endedBy") or "") == "clarify" and not any(
            call["name"] == "meta.clarify" for call in actual
        ):
            clarify = next(
                (event["data"] for event in outcome["events"] if event.get("event") == "clarify"),
                {},
            )
            actual.append(
                {
                    "name": "meta.clarify",
                    "args": {
                        "question": clarify.get("question") or "",
                        "missing": clarify.get("missing") or [],
                    },
                    "ok": True,
                    "duration_ms": 0,
                }
            )
        expected = list(expect["tool_calls"])

        ended_by = str(done.get("endedBy") or "")
        tool_calls_ok = metrics.tool_calls_match(expected, actual)
        final_state_ok = _check_final_state(expect.get("final_state") or {}, pre, post)
        must_not_ok = _check_must_not_call(expect.get("must_not_call") or [], actual)

        guard_blocked = _guard_blocked(steps, done)
        hallucinated = _has_hallucinated_id(actual, pre, post) and not guard_blocked
        safety_attempted = _count_must_not_calls(expect.get("must_not_call") or [], actual)

        llm_steps = [s for s in steps if s.get("kind") == "LLM"]
        tool_steps = [s for s in steps if s.get("kind") == "TOOL"]
        tokens_in = sum(int(s.get("tokensIn") or 0) for s in llm_steps)
        tokens_out = sum(int(s.get("tokensOut") or 0) for s in llm_steps)
        llm_ms = sum(int(s.get("durationMs") or 0) for s in llm_steps)
        tool_ms = sum(int(s.get("durationMs") or 0) for s in tool_steps)
        model = next(
            (str((s.get("detail") or {}).get("model")) for s in reversed(llm_steps)
             if (s.get("detail") or {}).get("model")),
            self.model,
        )
        prompt_version = str(trace.get("promptVersion") or "")
        tool_failures = [
            f"{s.get('name')}: {(s.get('detail') or {}).get('code') or 'FAILED'}"
            for s in tool_steps
            if not bool(s.get("ok", True))
        ]

        final_state = {"tasks": post, "count": len(post)}
        row = CaseResult(
            case_id=case.id,
            group=case.group,
            passed=bool(tool_calls_ok and final_state_ok and must_not_ok),
            tool_calls_ok=tool_calls_ok,
            final_state_ok=final_state_ok,
            must_not_call_ok=must_not_ok,
            tool_calls_expected=expected,
            tool_calls_actual=actual,
            final_state=final_state,
            core_task_count=len(post),
            latency_ms=int(outcome["latency_ms"]),
            latency_excluded=ended_by in metrics.LATENCY_EXCLUDED_ENDED_BY,
            cost_yuan=float(done.get("costYuan") or trace.get("costYuan") or 0.0),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            degraded=bool(done.get("degraded") or trace.get("degraded")),
            ended_by=ended_by,
            trace_id=outcome["trace_id"],
            prompt_version=prompt_version,
            model=model,
            tags=list(case.tags),
            tool_steps=[
                {
                    "name": s.get("name"),
                    "duration_ms": int(s.get("durationMs") or 0),
                    "ok": bool(s.get("ok", True)),
                    "args": (s.get("detail") or {}).get("args") or {},
                }
                for s in tool_steps
            ],
            llm_ms=llm_ms,
            tool_ms=tool_ms,
            queue_wait_ms=float(outcome.get("queue_wait_ms") or 0.0),
            steps=int(done.get("steps") or len(llm_steps)),
            guard_blocked=guard_blocked,
            hallucinated_id=hallucinated,
            safety_attempted=safety_attempted,
            error="；".join(tool_failures),
        )
        return row


# ---------------------------------------------------------------------------
# 流 / trace 解析
# ---------------------------------------------------------------------------


async def _read_sse(client: httpx.AsyncClient, stream_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    async with client.stream("GET", f"/stream/{stream_id}") as response:
        if response.status_code != 200:
            body = (await response.aread()).decode("utf-8", "replace")
            raise RuntimeError(f"GET /stream/{stream_id} 返回 {response.status_code}: {body[:200]}")
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                current["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                raw = line.split(":", 1)[1].strip()
                try:
                    current["data"] = json.loads(raw)
                except ValueError:
                    current["data"] = {"raw": raw}
            elif line == "" and current:
                events.append(current)
                if current.get("event") == "done":
                    break
                current = {}
    return events


async def _fetch_trace(client: httpx.AsyncClient, trace_id: str) -> dict[str, Any]:
    response = await client.get(f"/trace/{trace_id}")
    if response.status_code != 200:
        return {}
    payload = response.json()
    return payload.get("data") or {}


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------


def _check_final_state(expect: dict[str, Any], pre: list[dict[str, Any]], post: list[dict[str, Any]]) -> bool:
    pre_map = {task["id"]: task for task in pre}
    post_map = {task["id"]: task for task in post}

    if expect.get("unchanged"):
        if set(pre_map) != set(post_map):
            return False
        return all(_signature(pre_map[tid]) == _signature(post_map[tid]) for tid in pre_map)

    if "count_delta" in expect and len(post) - len(pre) != int(expect["count_delta"]):
        return False

    for spec in expect.get("tasks") or []:
        task = post_map.get(str(spec.get("id")))
        if task is None:
            return False
        if "status" in spec and task["status"] != str(spec["status"]).upper():
            return False
        if "priority" in spec and task["priority"] != str(spec["priority"]).upper():
            return False
        if "version_delta" in spec:
            before = pre_map.get(str(spec.get("id")))
            before_version = int(before.get("version") or 0) if before else 0
            if int(task.get("version") or 0) - before_version != int(spec["version_delta"]):
                return False
        if "due_date" in spec and _date_part(task.get("dueAt")) != str(spec["due_date"]):
            return False
        if "due_time" in spec and _time_part(task.get("dueAt")) != str(spec["due_time"]):
            return False
        if "rrule" in spec and task.get("rrule") != spec["rrule"]:
            return False
        if "tags_contain" in spec:
            have = set(task.get("tags") or [])
            if not set(spec["tags_contain"]) <= have:
                return False
        if "title_contains" in spec and str(spec["title_contains"]) not in str(task.get("title") or ""):
            return False

    created = [task for task in post if task["id"] not in pre_map]
    used: set[str] = set()
    for spec in expect.get("created") or []:
        match = next((task for task in created if task["id"] not in used and _created_matches(task, spec)), None)
        if match is None:
            return False
        used.add(match["id"])

    for task_id in expect.get("not_exists") or []:
        if str(task_id) in post_map:
            return False
    for task_id in expect.get("exists") or []:
        if str(task_id) not in post_map:
            return False
    return True


def _created_matches(task: dict[str, Any], spec: dict[str, Any]) -> bool:
    if "title_contains" in spec and str(spec["title_contains"]) not in str(task.get("title") or ""):
        return False
    if "title" in spec and task.get("title") != spec["title"]:
        return False
    if "due_date" in spec and _date_part(task.get("dueAt")) != str(spec["due_date"]):
        return False
    if "due_time" in spec and _time_part(task.get("dueAt")) != str(spec["due_time"]):
        return False
    if "priority" in spec and task["priority"] != str(spec["priority"]).upper():
        return False
    if "tags_contain" in spec and not set(spec["tags_contain"]) <= set(task.get("tags") or []):
        return False
    return True


def _signature(task: dict[str, Any]) -> tuple[Any, ...]:
    return (
        task.get("id"),
        task.get("title"),
        task.get("status"),
        task.get("priority"),
        task.get("startAt"),
        task.get("dueAt"),
        task.get("estimateMin"),
        task.get("parentId"),
        task.get("rrule"),
        tuple(task.get("tags") or []),
        int(task.get("version") or 0),
    )


def _date_part(value: Any) -> str:
    parsed = metrics._parse_datetime(value)  # noqa: SLF001 - 同一包的确定性小工具
    return parsed.date().isoformat() if parsed else ""


def _time_part(value: Any) -> str:
    parsed = metrics._parse_datetime(value)  # noqa: SLF001
    return parsed.strftime("%H:%M") if parsed else ""


def _check_must_not_call(rules: list[Any], actual: list[dict[str, Any]]) -> bool:
    for rule in rules:
        if isinstance(rule, str):
            if any(call["name"] == rule for call in actual):
                return False
            continue
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("name") or rule.get("tool") or "")
        constraints = {k: v for k, v in rule.items() if k not in {"name", "tool"}}
        for call in actual:
            if call["name"] == name and metrics.fields_match(constraints, call.get("args") or {}):
                return False
    return True


def _count_must_not_calls(rules: list[Any], actual: list[dict[str, Any]]) -> int:
    count = 0
    for rule in rules:
        if isinstance(rule, str):
            count += sum(1 for call in actual if call["name"] == rule)
        elif isinstance(rule, dict):
            name = str(rule.get("name") or rule.get("tool") or "")
            constraints = {k: v for k, v in rule.items() if k not in {"name", "tool"}}
            count += sum(
                1
                for call in actual
                if call["name"] == name and metrics.fields_match(constraints, call.get("args") or {})
            )
    return count


def _guard_blocked(steps: list[dict[str, Any]], done: dict[str, Any]) -> bool:
    names = {str(step.get("name")) for step in steps if step.get("kind") == "GUARD"}
    code = str(done.get("errorCode") or "")
    return bool(names & _GUARD_BLOCK_ROWS) or code in _GUARD_BLOCK_CODES


def _has_hallucinated_id(
    actual: list[dict[str, Any]], pre: list[dict[str, Any]], post: list[dict[str, Any]]
) -> bool:
    known = {str(task["id"]) for task in pre} | {str(task["id"]) for task in post}
    for call in actual:
        for task_id in _iter_task_ids(call.get("args") or {}):
            if task_id and task_id not in known and task_id.startswith("t_"):
                return True
    return False


def _iter_task_ids(args: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key, value in args.items():
        if key in {"task_id", "parent_id", "id"} and isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            found.extend(_iter_task_ids(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found.extend(_iter_task_ids(item))
    return found


def _enrich_attempts(
    attempts: list[dict[str, Any]], steps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """用 trace 的 TOOL 行补 ok / duration；被 guard 拦下的调用没有 TOOL 行，保持 ok=False。"""
    tool_steps = [step for step in steps if step.get("kind") == "TOOL"]
    used: set[int] = set()
    enriched: list[dict[str, Any]] = []
    for attempt in attempts:
        name = attempt["name"]
        match_index = next(
            (
                index
                for index, step in enumerate(tool_steps)
                if index not in used and step.get("name") == name
            ),
            None,
        )
        row = dict(attempt)
        if match_index is None:
            row.update({"ok": False, "duration_ms": 0})
        else:
            used.add(match_index)
            step = tool_steps[match_index]
            row.update({"ok": bool(step.get("ok", True)), "duration_ms": int(step.get("durationMs") or 0)})
        enriched.append(row)
    return enriched


# ---------------------------------------------------------------------------
# fake 剧本
# ---------------------------------------------------------------------------


def _fake_script(case: Case, resolved: dict[str, Any]) -> list[Any]:
    """把期望调用变成 FakeLLM 的可执行剧本。

    这不是"评测模型"：fake 模式只验证 runner/断言/报告流水线。
    真实能力必须 `--llm live --core live` 跑；fake 的数字不写进任何结论。
    """
    script: list[Any] = []
    for item in resolved["expect"]["tool_calls"]:
        name = str(item["name"])
        args = _concretize(item.get("args") or {})
        if name == "meta.clarify" and not args:
            args = {"question": f"请补充信息：{case.input[:60]}", "missing": []}
        script.append(call(name, **args))
    script.append(say("已完成。"))
    return script


def _concretize(value: Any) -> Any:
    """把断言用的窄标记（contains/date/lte/...）变成工具能接收的具体值。"""
    if isinstance(value, dict):
        if "contains" in value:
            wanted = value["contains"]
            return [_concretize(item) for item in wanted] if isinstance(wanted, list) else _concretize(wanted)
        if "date" in value:
            return f"{value['date']}T18:00:00+08:00"
        if "datetime" in value:
            return str(value["datetime"])
        if "lte" in value:
            return value["lte"]
        if "gte" in value:
            return value["gte"]
        if "not_null" in value:
            return "present"
        if "regex" in value:
            return "x"
        if "any" in value:
            return "x"
        return {key: _concretize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_concretize(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# 冻结 spec 的简化入口
# ---------------------------------------------------------------------------


def _result_from_dict(payload: dict[str, Any]) -> CaseResult:
    valid = {field.name for field in fields(CaseResult)}
    return CaseResult(**{key: value for key, value in payload.items() if key in valid})


def run_case(case: Case, *, llm: str = "fake", core: str = "fake") -> CaseResult:
    """单条用例（同步入口，冻结 spec 用法：`run_case(ds.cases[0])`）。"""
    dataset = Dataset(version=case.source or "v1", cases=[case], path=Path("."))
    runner = Runner(dataset, llm=llm, core=core, cache=False)
    return asyncio.run(runner.run([case])).rows[0]


def run_dataset(
    path: str | Path,
    *,
    limit: int | None = None,
    llm: str = "fake",
    core: str = "fake",
) -> RunSummary:
    dataset = load_dataset(path)
    cases = dataset.cases[:limit] if limit else dataset.cases
    runner = Runner(dataset, llm=llm, core=core, cache=False)
    return asyncio.run(runner.run(cases))


__all__ = ["CaseResult", "Runner", "RunSummary", "run_case", "run_dataset"]
