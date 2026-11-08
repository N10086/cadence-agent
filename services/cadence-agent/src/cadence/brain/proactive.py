"""主动建议（SP3 挂载点，SP2 返回空列表）。

返回空列表而不是抛错，因为"没有建议"是一个合法的正常状态；
但要在类型上明确它是"未实现"，避免被误读成"算过了，确实没有建议"。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Suggestion:
    kind: str
    message: str
    task_ids: list[str]


SP2_IMPLEMENTED = False


async def suggest(*, tasks: list[dict], now=None) -> list[Suggestion]:
    """SP3 之前永远返回空。

    `SP2_IMPLEMENTED=False` 是给测试和评测用的哨兵：
    SP4 统计 `proactive_hit_rate` 时必须先看这个标志，否则会把"没实现"算成"没命中"。
    """
    return []
