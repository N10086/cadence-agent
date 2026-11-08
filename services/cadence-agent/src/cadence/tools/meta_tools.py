"""控制流工具：meta.clarify / meta.finish。

它们不是业务工具，价值在于把「提问」和「结束」变成模型**必须显式做出的选择**，
从而可以被评测统计（`clarification_rate` / `false_guess_rate`）。

`meta.finish` 的 `actions_taken` 会被 guard 校验：
**这里提到的每个动作，必须真的发生过** —— 防止模型编造"我已经帮你排好了"。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..guard.permissions import Permission
from ..runtime.context import TurnContext
from .base import Tool


class ClarifyArgs(BaseModel):
    question: str = Field(..., min_length=1, max_length=200, description="一个具体、可回答的问题")
    options: list[str] | None = Field(None, max_length=4, description="可选项，最多 4 个")
    missing: list[str] = Field(default_factory=list, description="缺失的槽位名，用于统计哪类信息最常缺")


class FinishArgs(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000, description="给用户的最终回复，中文，简洁")
    actions_taken: list[str] = Field(
        default_factory=list,
        description="本轮**实际执行**的动作摘要。禁止写没做过的事。",
    )


async def _clarify(args: ClarifyArgs, ctx: TurnContext) -> dict[str, Any]:
    ctx.clarify_count += 1
    return {
        "control": "clarify",
        "question": args.question,
        "options": args.options or [],
        "missing": args.missing,
    }


async def _finish(args: FinishArgs, ctx: TurnContext) -> dict[str, Any]:
    # 只做「存在性」校验：模型声称做过的动作里，至少要能在本轮动作记录里找到痕迹。
    claimed = [a for a in args.actions_taken if a.strip()]
    unverified = [a for a in claimed if not _was_done(a, ctx)]
    return {
        "control": "finish",
        "message": args.message,
        "actions_taken": claimed,
        "unverified": unverified,
    }


#: 只读动作不算"承诺"：它们的证据在 trace 里，不需要也不应该进 actions_taken
READ_HINTS = ("查询", "查看", "搜索", "列出", "找了", "看看", "查了", "找到", "检查", "确认了一下")


def _was_done(claim: str, ctx: TurnContext) -> bool:
    """宽松匹配：动作摘要里出现了任务标题或工具名就算做过。

    严格字符串相等会把"创建任务「买牛奶」"和"创建了买牛奶"判成两件事，
    误报比漏报更伤（会让正常的回复被拦下来）。

    另外，"我查了一下今天有什么任务"这类**只读**描述永远算真话 ——
    读操作不进 `actions_taken`（那是给用户看"改了什么"的），
    但它在 trace 里一定留下了痕迹。
    """
    text = claim.strip()
    if any(hint in text for hint in READ_HINTS):
        return True
    for done in ctx.actions_taken:
        if text and (text in done or done in text):
            return True
    for tool_name in ("task.create", "task.update", "task.complete", "task.delete", "task.split"):
        if tool_name in text and ctx.actions_taken:
            return True
    return False


TOOLS: list[Tool] = [
    Tool(
        name="meta.clarify",
        description=(
            "需要用户补充信息时调用。**一次只问一个问题** —— 问三个问题用户会烦，也无法评测。"
            "有歧义时优先问，不要猜。missing 填缺失的信息类别（如 title / date / task_id）。"
            "用户要求取消任务时也用它：当前版本不支持取消，要在问题里明确说明，"
            "并给出「标记完成」或「删除」的替代选项。"
        ),
        args_model=ClarifyArgs,
        handler=_clarify,
        permission=Permission.CONTROL,
    ),
    Tool(
        name="meta.finish",
        description=(
            "本轮完成时调用，给出最终回复。"
            "actions_taken 里**只能写真的执行过的动作**，编造会被拦下。"
            "如果什么都没做（比如只是回答了一个问题），actions_taken 可以是空列表。"
        ),
        args_model=FinishArgs,
        handler=_finish,
        permission=Permission.CONTROL,
    ),
]
