"""排期类工具：plan.reschedule / plan.apply。

**SP2 阶段是桩**，真正的求解在 SP3 的 `brain/solver`。

为什么保留注册：契约要冻结。工具清单里有它、schema 有它，
模型才知道"系统具备这个能力"，只是当前返回结构化拒绝而不是 500。
这样 SP3 上线时不需要改工具层，只改 handler。

`plan.reschedule` 刻意没有对应的 Core 接口 —— 求解在 Python 侧完成，Core 保持"哑"。
这条边界守住了，Core 才能被独立测试和复用。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..guard.permissions import Permission
from ..runtime.context import TurnContext
from ..runtime.errors import ErrorCode, ToolError
from .base import Tool


class TimeWindow(BaseModel):
    start: str = Field(..., description="起始时间（ISO8601，含时区）")
    end: str = Field(..., description="结束时间（ISO8601，含时区），**不含**该时刻")


class RescheduleArgs(BaseModel):
    unavailable: list[TimeWindow] = Field(
        default_factory=list, description="不可用时段，如病假 [周三00:00, 周五24:00)"
    )
    movable: str = Field("all", description="all / low_priority_only / none")
    pinned_task_ids: list[str] = Field(
        default_factory=list, description="用户明确要求不动的任务 id，必须来自本轮 task.search"
    )
    horizon_days: int = Field(14, ge=1, le=60, description="向后看多少天")
    dry_run: bool = Field(True, description="纯计算，本来就不落库")


class PlanApplyArgs(BaseModel):
    plan_id: str = Field(..., description="plan.reschedule 返回的方案 id")
    confirm_token: str = Field(..., description="用户确认后得到的令牌")


async def _reschedule(args: RescheduleArgs, ctx: TurnContext) -> dict[str, Any]:
    raise ToolError(
        ErrorCode.NOT_IMPLEMENTED,
        "重排期求解器属于 SP3（cadence-brain），当前版本尚未实现。"
        "如果需要，我可以先帮你查清楚哪些时段被占用（schedule.busy），"
        "或者把任务按优先级列出来，由你来决定怎么挪。",
        details={"tool": "plan.reschedule", "stage": "SP2-stub"},
        suggestions=[{"tool": "schedule.busy", "args": {"from": "", "to": ""}}],
    )


async def _apply(args: PlanApplyArgs, ctx: TurnContext) -> dict[str, Any]:
    pending = ctx.confirms.peek(args.confirm_token)
    if pending is None:
        raise ToolError(
            ErrorCode.PLAN_STALE,
            "这个方案已经过期或被用过了，需要重新生成。",
            details={"plan_id": args.plan_id},
        )
    raise ToolError(
        ErrorCode.NOT_IMPLEMENTED,
        "方案执行依赖 SP3 的求解结果，当前版本无法生成可执行的方案。",
        details={"tool": "plan.apply", "stage": "SP2-stub"},
    )


TOOLS: list[Tool] = [
    Tool(
        name="plan.reschedule",
        description=(
            "在给定约束下重排一段时间的任务安排，返回 diff（moved / kept / cannot_fit）。"
            "纯计算，不落库。必须诚实返回排不下的任务，而不是硬塞。"
        ),
        args_model=RescheduleArgs,
        handler=_reschedule,
        permission=Permission.READ,
        supports_dry_run=True,
        exposed=False,  # SP3 才实现；未实现却暴露给模型 = 让它去撞墙
    ),
    Tool(
        name="plan.apply",
        description=(
            "执行一份已经生成并被用户确认的排期方案。**破坏性操作，必须确认。**"
            "执行前会校验方案是否过期、任务 version 是否变过。"
        ),
        args_model=PlanApplyArgs,
        handler=_apply,
        permission=Permission.DESTRUCTIVE,
        exposed=False,  # 同上
    ),
]
