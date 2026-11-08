"""工具类别路由（两级注入）。

**为什么要有这一层**：10 个工具的完整 schema ≈ 2500 token，每一轮都发。
但一轮通常只需要其中一类：查询只要 read，单条改只要 read+write。
先把"需要哪几类"问出来，再只把那一类的工具 schema 发过去，能显著缩小上下文。

**两级的边界**：
- 第一级只看「类别摘要」——4 段话，不含任何具体工具；
- 第二级只发「被选中类别的具体工具」——类别摘要不再出现（它已经完成使命）。

**每轮重新选**：多步任务里，第 1 步可能要 read，第 2 步要 write。
所以路由发生在 loop 的每一轮开始，而不是一轮任务只选一次。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..llm.base import LLMClient, LLMResponse
from .base import Tool

#: 与 `guard.permissions.Permission` 一一对应（4 类）
ALL_CATEGORIES: tuple[str, ...] = ("read", "write", "destructive", "control")

#: 第一级注入上下文的就是这四段摘要；第二级不会带上它们。
CATEGORY_SUMMARIES: dict[str, str] = {
    "read": "只读：搜索/查看任务、查忙闲日历。不修改任何数据。",
    "write": "单条写入：新建任务、改字段、开始、完成、拆分。会改变任务数据。",
    "destructive": "破坏性：删除任务。不可撤销，必须先让用户确认。",
    "control": "控制流：向用户提问澄清、结束本轮并汇报。不碰业务数据。",
}

#: 第一级 prompt：只给类别摘要，要求模型输出一个 JSON。
ROUTE_SYSTEM_PROMPT = (
    "你是工具路由。根据用户这一轮的请求，判断需要哪几类工具。\n"
    "可选类别：\n"
    + "\n".join(f"- {name}：{text}" for name, text in CATEGORY_SUMMARIES.items())
    + "\n\n判断规则：\n"
    "1. 纯查询 → 只要 read。\n"
    "2. 要改某一条任务 → read + write（先用 read 拿到 id/version）。\n"
    "3. 要删除 → read + destructive。\n"
    "4. 需要反问用户 → control。\n"
    "5. 一句话里有多个动作 → 把涉及的类别都选上。\n"
    "control 会自动附加，不需要写。\n"
    '只输出 JSON，不要解释：{"categories":["read","write"]}'
)

#: 第一级调用只允许很短：它只输出一个类别数组。
ROUTE_MAX_TOKENS = 24


def categories_for_intent(intent: str) -> list[str]:
    """规则兜底（fake 模式 / 第一级失败时用）。与 `brain.intent` 的 6 类对齐。"""
    mapping = {
        "query": ["read"],
        "mutate": ["read", "write"],
        "multi": ["read", "write"],
        "destructive": ["read", "destructive"],
        "chitchat": [],
        "unknown": list(ALL_CATEGORIES),
    }
    return list(mapping.get(intent, ALL_CATEGORIES))


def parse_categories(text: str) -> list[str]:
    """从模型输出里抠出类别；解析不出来返回空列表（调用方会回退到全量）。"""
    raw = (text or "").strip()
    if not raw:
        return []

    # 优先按 JSON 解析（模型可能包 ```json 或加解释）
    match = re.search(r"\{.*\}", raw, re.S)
    if match:
        try:
            payload = json.loads(match.group(0))
            values = payload.get("categories")
            if isinstance(values, list):
                return _valid(values)
            if isinstance(values, str):
                return _valid([values])
        except (json.JSONDecodeError, AttributeError):
            pass

    # 退路：直接扫类别名（顺序按出现顺序去重）
    found = [name for name in ALL_CATEGORIES if re.search(rf"\b{name}\b", raw, re.I)]
    return found


def _valid(values: list[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        name = str(value).strip().lower()
        if name in ALL_CATEGORIES and name not in out:
            out.append(name)
    return out


def normalize(categories: list[str] | None) -> list[str]:
    """去重 + 补 control（没有 control，模型既问不了问题也结束不了）。"""
    out: list[str] = []
    for value in categories or []:
        name = str(value).strip().lower()
        if name in ALL_CATEGORIES and name not in out:
            out.append(name)
    if "control" not in out:
        out.append("control")
    return out


def merge_categories(parsed: list[str], intent: str) -> list[str]:
    """合并「模型选出的类别」与「意图规则」，并处理两种病态输出。

    两条兜底，都是实测踩出来的：
    1. 只选出 `control`（例如把"新建一个任务"误判成"需要反问"）：这时模型
       连 task.search / task.create 都拿不到，只能空转。直接退回全量。
    2. 模型漏选（例如查询只给了 control）：与 intent 规则取并集，保证
       这一类任务至少拿到它必需的工具。
    """
    parsed = [c for c in (parsed or []) if c in ALL_CATEGORIES]
    if not parsed or parsed == ["control"]:
        return list(ALL_CATEGORIES)

    merged = list(parsed)
    for name in categories_for_intent(intent):
        if name not in merged:
            merged.append(name)
    return merged


async def choose_categories(
    client: LLMClient,
    *,
    user_message: str,
    step: int,
    actions: list[str] | None = None,
) -> tuple[list[str], LLMResponse]:
    """第一级：用一次极短的模型调用选类别。返回 (类别, 原始响应供计费/追踪)。"""
    context = f"用户请求：{user_message}\n当前是第 {step} 步。"
    if actions:
        context += "\n已完成的动作：" + "；".join(actions[-5:])
    response = await client.chat(
        [
            {"role": "system", "content": ROUTE_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        None,
        temperature=0.0,
        max_tokens=ROUTE_MAX_TOKENS,
    )
    return parse_categories(response.text), response


def tools_for_categories(tools: list[Tool], categories: list[str]) -> list[Tool]:
    wanted = set(categories)
    return [tool for tool in tools if tool.permission.value in wanted]
