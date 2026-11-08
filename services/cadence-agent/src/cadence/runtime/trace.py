"""全链路 trace。

写文件，不写数据库（PRD 02 §4）：trace 是只追加、不查询的冷数据，
写库只会拖慢主链路、撑大备份。

落盘形态：`traces/2026-11-04/tr_9b2c7f80.jsonl`，**每次 LLM / 工具调用各一行**。

为什么按 trace_id 分文件而不是按天一个大文件：
一次对话一个文件，`/trace/{id}` 直接打开对应文件，不用扫全表；
出问题把单个文件发给别人就能复现。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KIND_LLM = "LLM"
KIND_TOOL = "TOOL"
KIND_GUARD = "GUARD"
#: 新增：降级/熔断/预算中止。PRD 的 kind 只有三种，但 SP4 要统计 degraded_rate，
#: 没有这一类就只能靠 detail 猜，指标算不准。
KIND_DEGRADE = "DEGRADE"


@dataclass
class TraceStep:
    seq: int
    kind: str
    name: str
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    ok: bool = True
    detail: dict[str, Any] = field(default_factory=dict)
    at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class TraceRecorder:
    """一次对话一个实例。每次 add 立即落盘 —— 进程崩了也能留下已发生的步骤。"""

    def __init__(self, trace_id: str, trace_dir: Path, *, session_id: str = "", prompt_version: str = "") -> None:
        self.trace_id = trace_id
        self.trace_dir = Path(trace_dir)
        self.session_id = session_id
        self.prompt_version = prompt_version
        self.steps: list[TraceStep] = []
        self._closed = False
        self._path = self.trace_dir / datetime.now().strftime("%Y-%m-%d") / f"{trace_id}.jsonl"
        self._ensure()

    # ------------------------------------------------------------------

    def _ensure(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            header = {
                "trace_id": self.trace_id,
                "session_id": self.session_id,
                "prompt_version": self.prompt_version,
                "started_at": _now_iso(),
                "kind": "META",
            }
            self._path.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")

    def add(
        self,
        kind: str,
        name: str,
        *,
        duration_ms: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        ok: bool = True,
        detail: dict[str, Any] | None = None,
    ) -> TraceStep:
        step = TraceStep(
            seq=len(self.steps) + 1,
            kind=kind,
            name=name,
            duration_ms=duration_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            ok=ok,
            detail=detail or {},
            at=_now_iso(),
        )
        self.steps.append(step)
        if not self._closed:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(step.to_json() + "\n")
        return step

    def close(self, *, cost_yuan: float = 0.0, total_ms: int = 0, degraded: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        self._sync_header_version()
        footer = {
            "kind": "SUMMARY",
            "trace_id": self.trace_id,
            "prompt_version": self.prompt_version,
            "steps": len(self.steps),
            "cost_yuan": round(cost_yuan, 6),
            "total_ms": total_ms,
            "degraded": degraded,
            "ended_at": _now_iso(),
        }
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(footer, ensure_ascii=False) + "\n")

    def _sync_header_version(self) -> None:
        """META 行是在知道 prompt 版本之前写的。

        收尾时把它补齐，否则每个 trace 文件的第一行都是 `"prompt_version": ""`，
        扫文件做归因的人会以为版本丢了。
        """
        if not self.prompt_version:
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if not lines:
            return
        try:
            header = json.loads(lines[0])
        except json.JSONDecodeError:
            return
        if header.get("kind") != "META" or header.get("prompt_version") == self.prompt_version:
            return
        header["prompt_version"] = self.prompt_version
        lines[0] = json.dumps(header, ensure_ascii=False)
        self._path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def view(self) -> dict[str, Any]:
        """给 `GET /trace/{id}` 用的结构（对齐 docs/接口文档.md §7.4）。"""
        total_ms = sum(s.duration_ms for s in self.steps)
        cost = 0.0
        for s in self.steps:
            if s.kind == KIND_LLM:
                cost += float(s.detail.get("cost_yuan") or 0.0)
        return {
            "traceId": self.trace_id,
            "sessionId": self.session_id,
            "totalMs": total_ms,
            "costYuan": round(cost, 6),
            "steps": [
                {
                    "seq": s.seq,
                    "kind": s.kind,
                    "name": s.name,
                    "durationMs": s.duration_ms,
                    "tokensIn": s.tokens_in,
                    "tokensOut": s.tokens_out,
                    "ok": s.ok,
                    "detail": s.detail,
                }
                for s in self.steps
            ],
        }


# ---------------------------------------------------------------------------
# 读取（/trace/{id} 与 trace 查看器共用）
# ---------------------------------------------------------------------------


def find_trace_file(trace_dir: Path, trace_id: str) -> Path | None:
    root = Path(trace_dir)
    if not root.exists():
        return None
    safe = "".join(ch for ch in trace_id if ch.isalnum() or ch in "-_")
    if safe != trace_id:
        return None
    direct = list(root.glob(f"*/{safe}.jsonl"))
    if direct:
        return direct[0]
    return None


def _normalize_step(row: dict[str, Any]) -> dict[str, Any]:
    """JSONL 里存的是 snake_case（文件要紧凑），对外契约是 camelCase。

    这个转换漏过一次：`/trace/{id}` 直接把原始行返回，前端按 `durationMs` 取值全是
    undefined。测试只断言了"steps 非空"，没断言字段名，所以没抓到 ——
    现在有专门的测试盯这个。
    """
    return {
        "seq": row.get("seq"),
        "kind": row.get("kind"),
        "name": row.get("name"),
        "durationMs": row.get("durationMs", row.get("duration_ms", 0)),
        "tokensIn": row.get("tokensIn", row.get("tokens_in", 0)),
        "tokensOut": row.get("tokensOut", row.get("tokens_out", 0)),
        "ok": row.get("ok", True),
        "detail": row.get("detail") or {},
        "at": row.get("at", ""),
    }


def load_trace(trace_dir: Path, trace_id: str) -> dict[str, Any] | None:
    path = find_trace_file(trace_dir, trace_id)
    if path is None:
        return None
    meta: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = row.get("kind")
        if kind == "META":
            meta = row
        elif kind == "SUMMARY":
            summary = row
        else:
            steps.append(_normalize_step(row))

    return {
        "traceId": trace_id,
        "sessionId": meta.get("session_id", ""),
        "promptVersion": summary.get("prompt_version") or meta.get("prompt_version", ""),
        "startedAt": meta.get("started_at", ""),
        "totalMs": summary.get("total_ms", sum(s.get("durationMs", 0) for s in steps)),
        "costYuan": summary.get("cost_yuan", 0.0),
        "degraded": summary.get("degraded", False),
        "steps": steps,
    }


def format_timeline(trace: dict[str, Any]) -> str:
    """`python -m cadence.trace <id>` 的终端输出。面试时直接翻给人看。"""
    lines = [
        f"trace {trace['traceId']}  session={trace.get('sessionId') or '-'}  "
        f"steps={len(trace['steps'])}  {trace['totalMs']}ms  ¥{trace['costYuan']:.4f}"
        + ("  [DEGRADED]" if trace.get("degraded") else ""),
        "-" * 78,
    ]
    for s in trace["steps"]:
        flag = "ok " if s.get("ok", True) else "ERR"
        tokens = ""
        if s.get("tokensIn") or s.get("tokensOut"):
            tokens = f" in={s.get('tokensIn', 0)} out={s.get('tokensOut', 0)}"
        lines.append(
            f"{s['seq']:>2}. [{flag}] {s['kind']:<7} {s['name']:<22} "
            f"{s.get('durationMs', 0):>5}ms{tokens}"
        )
        detail = s.get("detail") or {}
        if detail.get("error"):
            lines.append(f"        error: {detail['error']}")
        elif detail.get("summary"):
            lines.append(f"        {detail['summary']}")
    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def new_trace_id() -> str:
    return f"tr_{int(time.time() * 1000):x}{_rand_hex(4)}"


def new_stream_id() -> str:
    return f"st_{_rand_hex(6)}"


def _rand_hex(n: int) -> str:
    import secrets

    return secrets.token_hex(n)
