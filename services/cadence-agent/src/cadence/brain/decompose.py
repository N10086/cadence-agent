"""任务分解（SP3 挂载点，SP2 是桩）。

桩的正确做法是**显式失败**，而不是返回一个看起来合理的空结果。
返回空结果会让模型以为"分解完了"，然后编造子任务 —— 那是最坏的情况。
"""

from __future__ import annotations

from dataclasses import dataclass

from .constraints import NotSupported


@dataclass
class Decomposition:
    parent_title: str
    children: list[dict]
    rationale: str = ""


async def decompose(goal: str, *, now=None) -> Decomposition:
    raise NotSupported(
        "任务分解属于 SP3（cadence-brain）。SP2 阶段请直接让用户描述具体子任务，"
        "或用 task.split 传入用户口述的子任务列表。"
    )
