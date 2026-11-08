"""日程类工具：schedule.busy。

注意查询串里的 `+` 必须编码成 `%2B`（Core 用 OffsetDateTime 解析）。
httpx 的 params 编码走 `quote` 而不是 `quote_plus`，所以 `+08:00` 会自动变成 `%2B08:00`。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..guard.permissions import Permission
from ..runtime.context import TurnContext
from ..runtime.errors import ErrorCode, ToolError
from .base import Tool


class BusyArgs(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(..., alias="from", description="查询起始时间（ISO8601，含时区）")
    to: datetime = Field(..., description="查询结束时间（ISO8601，含时区），必须晚于起始时间")


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat(timespec="seconds")


async def _busy(args: BusyArgs, ctx: TurnContext) -> dict[str, Any]:
    if args.to <= args.from_:
        raise ToolError(
            ErrorCode.VALIDATION_ERROR,
            "to 必须晚于 from",
            details={"from": _iso(args.from_), "to": _iso(args.to)},
        )
    if args.to - args.from_ > timedelta(days=90):
        raise ToolError(ErrorCode.VALIDATION_ERROR, "查询区间不能超过 90 天",

                        details={"days": (args.to - args.from_).days})

    data = await ctx.core.request(
        "GET",
        "/api/v1/calendar/busy",
        params={"from": _iso(args.from_), "to": _iso(args.to)},
        trace_id=ctx.trace_id,
        tool_name="schedule.busy",
    )
    windows = (data or {}).get("windows") or []
    total_min = sum(int(w.get("minutes") or 0) for w in windows)
    return {
        "windows": windows,
        "count": len(windows),
        "busy_minutes": total_min,
        "from": _iso(args.from_),
        "to": _iso(args.to),
    }


TOOLS: list[Tool] = [
    Tool(
        name="schedule.busy",
        description=(
            "查询某个时间段内已被任务占用的时间（已合并相邻区间）。"
            "排期类操作前先用它看看哪些时间不空。"
            "返回的是「忙」的区间，不是「空」的区间。"
        ),
        args_model=BusyArgs,
        handler=_busy,
        permission=Permission.READ,
    ),
]
