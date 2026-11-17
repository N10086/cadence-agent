"""三级记忆 + 滑动窗口。

**为什么是三级**（对应"记住多久"）：

```
L1 短期  recent_turns      最近若干轮原文，按 token 预算滑动窗口
L2 中期  summary           被窗口挤出去的轮次压成滚动摘要（抽取式，不花钱）
L3 长期  facts             跨轮保留的稳定事实（偏好/约束/别名），有上限、去重
```

**边界**：
- 不做向量检索（没有 embedding / 召回排序），L3 直接全量注入（≤10 条）；
- 摘要默认**抽取式**（把轮次压成"用户: …；助手: …"），不调模型；
  需要更高质量时可以把 `summary` 换成 fast 模型生成，但那是额外的钱和不确定性；
- 记忆只存在会话内，换 sessionId 就是全新；落盘靠 `SessionStore`（TTL 30 分钟）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..llm.cost import estimate_tokens
from .session import SessionState, Turn


@dataclass(frozen=True)
class MemoryConfig:
    window_tokens: int = 800
    max_turns: int = 8
    summary_chars: int = 300
    facts_limit: int = 10


def sliding_window(
    turns: list[Turn],
    *,
    window_tokens: int,
    max_turns: int,
    min_turns: int = 1,
) -> tuple[list[Turn], list[Turn]]:
    """从最新往回装，直到 token 预算 / 条数上限；至少保留最新 1 轮。

    返回 `(保留, 被挤出)`，两者都按时间正序。
    """
    if not turns:
        return [], []

    kept: list[Turn] = []
    used = 0
    for turn in reversed(turns):
        cost = max(1, estimate_tokens(turn.content))
        if kept and (used + cost > window_tokens or len(kept) >= max_turns):
            break
        kept.append(turn)
        used += cost

    # 至少保留最新 1 轮（哪怕它自己就超预算）
    if not kept:
        kept = [turns[-1]]
    kept.reverse()

    cut = len(turns) - len(kept)
    return kept, turns[:cut]


def _line(turn: Turn) -> str:
    who = "用户" if turn.role == "user" else "助手"
    text = " ".join(turn.content.split())
    return f"{who}: {text[:80]}"


def summarize(
    evicted: Iterable[Turn],
    previous: str = "",
    *,
    max_chars: int = 300,
) -> str:
    """滚动摘要：旧摘要 + 新挤出的轮次，超长时**保留最近的**部分。"""
    parts: list[str] = []
    if previous:
        parts.append(previous.rstrip("。；; "))
    for turn in evicted:
        parts.append(_line(turn))

    combined = "；".join(p for p in parts if p)
    if len(combined) <= max_chars:
        return combined

    # 超长：按分号切句，从尾部往前拼，保证最近的对话一定在
    sentences = [s for s in combined.split("；") if s]
    out: list[str] = []
    size = 0
    for sentence in reversed(sentences):
        if size + len(sentence) + 1 > max_chars:
            break
        out.append(sentence)
        size += len(sentence) + 1
    if not out:
        return combined[-max_chars:]
    return "…" + "；".join(reversed(out))


def remember_fact(
    session: SessionState,
    text: str,
    *,
    kind: str = "fact",
    limit: int = 10,
) -> bool:
    """写入一条长期记忆；重复（去空白后相同）返回 False。"""
    cleaned = " ".join(text.split()).strip()
    if not cleaned:
        return False
    for fact in session.facts:
        if fact.get("text") == cleaned:
            return False
    session.facts.append({"text": cleaned, "kind": kind})
    if len(session.facts) > limit:
        session.facts = session.facts[-limit:]
    session.touch()
    return True


def compact(session: SessionState, config: MemoryConfig) -> dict[str, Any]:
    """执行一次 L1 滑动 + L2 滚动摘要。返回给 trace 的统计。"""
    before = len(session.recent_turns)
    kept, evicted = sliding_window(
        session.recent_turns,
        window_tokens=config.window_tokens,
        max_turns=config.max_turns,
    )
    if not evicted:
        return {"kept": before, "evicted": 0, "summary_chars": len(session.summary)}

    session.summary = summarize(evicted, session.summary, max_chars=config.summary_chars)
    session.recent_turns = kept
    session.archived_turns += len(evicted)
    session.touch()
    return {
        "kept": len(kept),
        "evicted": len(evicted),
        "archived_total": session.archived_turns,
        "summary_chars": len(session.summary),
    }


def context_blocks(session: SessionState) -> list[str]:
    """拼进 prompt 的三级记忆块（顺序：摘要 → 长期记忆 → 约束）。"""
    blocks: list[str] = []
    if session.summary:
        blocks.append("【会话摘要】" + session.summary)
    if session.facts:
        lines = "\n".join(f"- {f.get('text', '')}" for f in session.facts)
        blocks.append("【长期记忆】\n" + lines)
    if session.constraints:
        import json

        blocks.append("【已确认的约束】" + json.dumps(session.constraints, ensure_ascii=False))
    return blocks
