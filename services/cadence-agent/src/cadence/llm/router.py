"""确定性模型路由。

**不是让模型自己选意图，而是判「这一轮难不难」。**

理由（PRD 02 §6.2）：七类意图分类错了整条链路就错；
两分类错了只是选错模型，代价是快一点或慢一点。
**架构由错误的代价决定，不由分类的完整性决定。**

只有一个 provider 时（当前只有 DeepSeek key），路由仍然工作：
它决定**用短提示还是长提示、给不给 few-shot**，并把 tier 写进 trace 供 SP4 统计。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .base import LLMClient


class Tier(str, Enum):
    FAST = "fast"
    REASON = "reason"


@dataclass(frozen=True)
class RouteDecision:
    tier: Tier
    reason: str  # 为什么这么判，进 trace 便于回溯
    rule: str  # 命中的规则名
    degraded: bool = False


# ---- 规则表（顺序即优先级） ----------------------------------------------

#: 多动作 / 需要规划的强信号。
#: 注意刻意**不包含**「所有 / 全部 / 这周」这类量词和时间范围：
#: 「列出所有任务」是一次简单查询，把它们算成复杂会让 fast 路径形同虚设。
COMPLEX_MARKERS = re.compile(
    r"(然后|之后|接着|再建|再帮|顺便|同时|并且|以及|还有|"
    r"重排|重新排|安排一下|规划|分解|拆成|拆解|准备面试)"
)

#: 纯查询信号
QUERY_MARKERS = re.compile(r"(有哪些|有什么|多少|列出|列一下|看看|查一下|查询|找一下|找找|是什么|情况|进度)")

#: 单条变更信号
MUTATE_MARKERS = re.compile(
    r"(标记完成|标记为完成|完成掉|完成了|勾掉|做完|"
    r"开始做|开始|开工|"
    r"删掉|删除|去掉|取消掉|取消|"
    r"新建|创建|加一个|新增|建一个|"
    r"改成|改为|改到|挪到|推迟|提前|置顶|固定)"
)

#: 危险动作信号（只用于路由判断，真正的拦截在 guard）
DANGER_MARKERS = re.compile(r"(删除|删掉|清空|全部删|批量删|清理)")

TIER_GAP_DEFAULT = 60  # 字符数：超过则倾向 REASON


def route(text: str, *, gap: int = TIER_GAP_DEFAULT) -> RouteDecision:
    """规则优先，纯函数，可单测。同一输入永远同一结果。"""
    msg = (text or "").strip()
    if not msg:
        return RouteDecision(Tier.FAST, "空消息", "empty")

    if COMPLEX_MARKERS.search(msg):
        return RouteDecision(Tier.REASON, "含多动作/跨天信号", "complex-marker")

    if DANGER_MARKERS.search(msg) and not QUERY_MARKERS.search(msg):
        # 危险操作本身不复杂，但值得走强模型把话说清楚
        return RouteDecision(Tier.REASON, "破坏性操作，值得更强的理解", "danger-marker")

    if QUERY_MARKERS.search(msg) and not MUTATE_MARKERS.search(msg):
        return RouteDecision(Tier.FAST, "纯查询", "pure-query")

    if MUTATE_MARKERS.search(msg):
        # 只命中一个变更动词 → 单条 CRUD
        hits = len(MUTATE_MARKERS.findall(msg))
        if hits <= 1:
            return RouteDecision(Tier.FAST, "单条变更", "single-crud")

    if len(msg) > gap:
        return RouteDecision(Tier.REASON, f"长句（{len(msg)} 字）且无明确信号", "long-text")

    return RouteDecision(Tier.FAST, "短句无复杂信号", "short-default")


COMPLEXITY_PROMPT = (
    "判断下面这句话是「一次简单的查或改」还是「需要多步规划」。"
    "只回答一个词：SIMPLE 或 COMPLEX。\n\n"
)


async def route_with_verdict(
    text: str,
    client: LLMClient,
    *,
    enabled: bool,
    gap: int = TIER_GAP_DEFAULT,
) -> RouteDecision:
    """规则拿不准时，用一次极短调用兜底（max_tokens=1 级别）。

    默认关闭（`ROUTER_LLM_VERDICT=false`）：只有一个 provider 时，
    这次调用花的钱和主调用一样贵，不划算。双模型时再打开。
    """
    decision = route(text, gap=gap)
    if not enabled or decision.rule in ("complex-marker", "pure-query", "single-crud"):
        return decision

    try:
        resp: Any = await client.chat(
            [{"role": "user", "content": COMPLEXITY_PROMPT + text}],
            None,
            temperature=0.0,
            max_tokens=4,
        )
        verdict = (resp.text or "").strip().upper()
        if "COMPLEX" in verdict and "SIMPLE" not in verdict:
            return RouteDecision(Tier.REASON, "模型判定 COMPLEX", "llm-verdict")
        return RouteDecision(Tier.FAST, "模型判定 SIMPLE", "llm-verdict")
    except Exception:  # noqa: BLE001 - 路由失败不该让整轮失败
        return RouteDecision(decision.tier, decision.reason + "（路由兜底失败，沿用规则）", decision.rule)
