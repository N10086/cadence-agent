"""错误分类。

PRD `05-tool-schema.md` §4 列了 10 个码，落地时发现必须补 4 个（共 14 个）：

| 新增 | 为什么 |
|---|---|
| `TASK_ID_NOT_IN_CONTEXT` | 白名单拦截发生在本地，不发往 Core，需要一个独立码（02 §7.2 提到但 05 §4 表里漏了） |
| `ILLEGAL_TRANSITION`    | Core 会返回 422「已完成的任务不能取消」这类，两处文档都没有对应码 |
| `LOOP_DETECTED`         | 指纹去重判定打转，与「步数超预算」是两种不同的失败，评测要分开统计 |
| `NOT_IMPLEMENTED`       | SP2 阶段 `plan.reschedule` / `task.split` 是桩，必须结构化拒绝而不是 500 |

`recoverable` 是这套设计里最要紧的字段：**false 的错误，Agent 不得重试** ——
这是防止「死循环烧钱」的最后一道闸。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    # ---- PRD 原有的 10 个 ----
    VALIDATION_ERROR = "VALIDATION_ERROR"
    TASK_ID_NOT_FOUND = "TASK_ID_NOT_FOUND"
    CONFLICT = "CONFLICT"
    DEPENDENCY_VIOLATION = "DEPENDENCY_VIOLATION"
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
    PLAN_STALE = "PLAN_STALE"
    HARD_CONSTRAINT_UNSATISFIABLE = "HARD_CONSTRAINT_UNSATISFIABLE"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    # ---- 落地时补的 4 个 ----
    TASK_ID_NOT_IN_CONTEXT = "TASK_ID_NOT_IN_CONTEXT"
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
    LOOP_DETECTED = "LOOP_DETECTED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    # ---- L2 补的 1 个 ----
    #: 单轮总超时（60s）。它不等于"模型读超时"：读超时是上游慢，这个是整轮（含规划+工具）
    #: 太久，必须收尾并把已完成的动作交代清楚。
    TURN_TIMEOUT = "TURN_TIMEOUT"
    # ---- L3 补的 2 个 ----
    #: 入口准入队列已满（64）。**不排队**：直接 503，让客户端退避，而不是把延迟堆到无限。
    OVERLOADED = "OVERLOADED"
    #: 同一会话的上一轮还没结束，等锁超过 20s。409 而不是 503：这是"这个会话忙"，不是"整机忙"。
    SESSION_BUSY = "SESSION_BUSY"


#: 不可恢复 —— 出现即中止，不得重试
NON_RECOVERABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.DEPENDENCY_VIOLATION,
        ErrorCode.HARD_CONSTRAINT_UNSATISFIABLE,
        ErrorCode.BUDGET_EXCEEDED,
        ErrorCode.ILLEGAL_TRANSITION,
        ErrorCode.LOOP_DETECTED,
        ErrorCode.NOT_IMPLEMENTED,
        ErrorCode.TURN_TIMEOUT,
    }
)

#: 给用户的默认话术（模型也可以用 suggestions 自己修）
DEFAULT_HINT: dict[ErrorCode, str] = {
    ErrorCode.VALIDATION_ERROR: "参数不合法，我会修正后重试一次。",
    ErrorCode.TASK_ID_NOT_FOUND: "没找到这个任务，我先查一下是不是叫别的名字。",
    ErrorCode.CONFLICT: "这个任务刚被改过，我重新读取最新版本再改。",
    ErrorCode.DEPENDENCY_VIOLATION: "存在循环依赖，需要你先调整任务关系。",
    ErrorCode.CONFIRM_REQUIRED: "这是不可逆操作，需要你确认。",
    ErrorCode.PLAN_STALE: "确认期间任务被改过了，需要重新生成方案。",
    ErrorCode.HARD_CONSTRAINT_UNSATISFIABLE: "在现有条件下排不下，我会说明原因。",
    ErrorCode.RATE_LIMITED: "请求太频繁，稍等一下。",
    ErrorCode.UPSTREAM_UNAVAILABLE: "任务服务暂时不可用。",
    ErrorCode.BUDGET_EXCEEDED: "这一步做不完，我先汇报已经完成的部分。",
    ErrorCode.TASK_ID_NOT_IN_CONTEXT: "我不该用这个 id，先重新查一遍。",
    ErrorCode.ILLEGAL_TRANSITION: "当前状态不允许这个操作。",
    ErrorCode.LOOP_DETECTED: "我似乎陷入了重复操作，先停下来汇报。",
    ErrorCode.NOT_IMPLEMENTED: "这个能力还没实现。",
    ErrorCode.TURN_TIMEOUT: "这一轮超时了，我先汇报已经完成的部分。",
    ErrorCode.OVERLOADED: "服务繁忙，排队已满，请稍后再试。",
    ErrorCode.SESSION_BUSY: "这个会话上一条消息还在处理，等它完成再发。",
}


@dataclass
class ToolError(Exception):
    """工具层 / 护栏层的统一错误。直接可序列化成模型可读的 JSON。

    `core_down` 与 `code` 是正交的两件事：前者回答"Core 这个依赖是不是挂了"，
    后者回答"这次调用错在哪"。L2 的 L3 降级必须看前者 —— 把"CORE 连接失败"
    误判成"这个工具不支持"会让整轮对话给出错误结论。
    """

    code: ErrorCode
    message: str
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] | None = None
    recoverable: bool | None = None
    core_down: bool = False

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.code.value}: {self.message}")
        if self.recoverable is None:
            self.recoverable = self.code not in NON_RECOVERABLE

    def to_dict(self) -> dict[str, Any]:
        """P4：错误是给模型看的，必须可行动。"""
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "recoverable": bool(self.recoverable),
                "suggestions": self.suggestions,
            },
        }
        if self.details:
            payload["error"]["details"] = self.details
        if self.core_down:
            payload["error"]["io"] = "core"
        return payload

    def to_tool_json(self) -> dict[str, Any]:
        """模型看到的版本：再省一层，不带 details 里的内部字段。"""
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "recoverable": bool(self.recoverable),
                "suggestions": self.suggestions,
                "hint": DEFAULT_HINT.get(self.code, ""),
            },
        }


class GuardRejected(ToolError):
    """护栏拦截：请求根本没发往 Core。"""

    def __init__(self, code: ErrorCode, message: str, **kw: Any) -> None:
        super().__init__(code=code, message=message, **kw)


class BudgetExceeded(ToolError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code=ErrorCode.BUDGET_EXCEEDED, message=message, details=details)
