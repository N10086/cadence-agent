"""意图分类（SP2 桩：关键词规则）。

SP3 会把这里换成"自然语言 → 约束模型"。SP2 只做两件事：
1. 判断**有没有歧义**（有就 clarify，不许猜）
2. 给路由一个粗线索

规则写得朴素是故意的：SP2 的验收是"护栏有效"，不是"理解准确"。
理解准确要等 SP3，而 SP3 的每一次改进都要在 SP4 的评测集上量出来。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Intent(str, Enum):
    QUERY = "query"  # 只读：有哪些任务
    MUTATE = "mutate"  # 单条变更
    MULTI = "multi"  # 多步
    DESTRUCTIVE = "destructive"
    CHITCHAT = "chitchat"
    UNKNOWN = "unknown"


#: 指代词 + 没有具体宾语 → 歧义
VAGUE_PRONOUN = re.compile(r"(那个|这个|它|那件事|刚才那个|上面那个)")
#: 万能用动词，没有宾语时必然歧义
VAGUE_VERB = re.compile(r"(搞一下|搞搞|处理一下|弄一下|弄弄|安排一下|弄好|搞定|看一下)")
QUERY = re.compile(r"(有哪些|有什么|多少|列出|列一下|看看|查一下|查询|找一下|进度|情况)")
MUTATE = re.compile(r"(标记完成|完成|做完|开始|删除|删掉|取消|新建|创建|加一个|建一个|改成|改到|挪到|推迟)")
MULTI = re.compile(r"(然后|之后|接着|再建|再帮|顺便|同时|并且)")
DESTRUCTIVE = re.compile(r"(删除|删掉|清空|全部删|清理)")
CHITCHAT = re.compile(r"^(你好|hi|hello|嗨|在吗|谢谢|thanks)", re.I)


@dataclass
class ClassifyResult:
    intent: Intent
    ambiguous: bool = False
    missing: list[str] = field(default_factory=list)
    question: str = ""
    note: str = ""


def classify(text: str) -> ClassifyResult:
    msg = (text or "").strip()
    if not msg:
        return ClassifyResult(Intent.UNKNOWN, ambiguous=True, missing=["message"], question="你想让我做什么？")

    if CHITCHAT.search(msg):
        return ClassifyResult(Intent.CHITCHAT, note="寒暄，不需要工具")

    if DESTRUCTIVE.search(msg):
        return ClassifyResult(Intent.DESTRUCTIVE, note="破坏性意图，需要确认")

    if MULTI.search(msg):
        return ClassifyResult(Intent.MULTI, note="多步意图")

    if QUERY.search(msg) and not MUTATE.search(msg):
        return ClassifyResult(Intent.QUERY, note="查询意图")

    if MUTATE.search(msg):
        # 变更动词 + 没有具体宾语 → 问清楚是哪个任务
        if VAGUE_PRONOUN.search(msg) or VAGUE_VERB.search(msg):
            return ClassifyResult(
                Intent.MUTATE,
                ambiguous=True,
                missing=["task_id"],
                question="你指的是哪个任务？我先把候选列出来。",
                note="有指代词或万能用动词，缺具体任务",
            )
        return ClassifyResult(Intent.MUTATE, note="单条变更")

    if VAGUE_PRONOUN.search(msg) or VAGUE_VERB.search(msg):
        return ClassifyResult(
            Intent.UNKNOWN,
            ambiguous=True,
            missing=["intent"],
            question="你想让我做什么？能具体说一个动作吗？",
            note="只有指代/万能动词",
        )

    return ClassifyResult(Intent.UNKNOWN, note="规则未命中，交给模型")


#: 哪些意图在缺信息时必须先问，而不是猜
MUST_CLARIFY = {Intent.MUTATE, Intent.UNKNOWN}
