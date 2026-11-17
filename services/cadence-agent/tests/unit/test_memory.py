"""三级记忆：滑动窗口 / 滚动摘要 / 长期事实。"""

from __future__ import annotations

from cadence.runtime.memory import (
    MemoryConfig,
    compact,
    context_blocks,
    remember_fact,
    sliding_window,
    summarize,
)
from cadence.runtime.session import SessionState, Turn


def _turn(role: str, content: str) -> Turn:
    return Turn(role=role, content=content)


def _long(text: str, n: int = 300) -> str:
    return (text * (n // len(text) + 1))[:n]


# ---------------------------------------------------------------------------
# L1 滑动窗口
# ---------------------------------------------------------------------------


def test_window_keeps_newest_within_token_budget() -> None:
    turns = [_turn("user", _long("a", 300)) for _ in range(3)]  # 每条约 100 token
    kept, evicted = sliding_window(turns, window_tokens=150, max_turns=10)
    assert len(kept) == 1, "预算只够一条时保留最新的"
    assert len(evicted) == 2
    assert kept[0] is turns[-1]
    assert [t is x for t, x in zip(evicted, turns[:2])]


def test_window_always_keeps_at_least_one_turn() -> None:
    turns = [_turn("user", _long("b", 600))]
    kept, evicted = sliding_window(turns, window_tokens=1, max_turns=10)
    assert len(kept) == 1 and evicted == []


def test_window_respects_hard_turn_cap() -> None:
    turns = [_turn("user", "短") for _ in range(10)]
    kept, evicted = sliding_window(turns, window_tokens=10_000, max_turns=3)
    assert len(kept) == 3
    assert len(evicted) == 7
    assert [t.content for t in kept] == ["短", "短", "短"]


# ---------------------------------------------------------------------------
# L2 滚动摘要
# ---------------------------------------------------------------------------


def test_summary_appends_and_stays_bounded() -> None:
    evicted = [_turn("user", "第一轮的问题"), _turn("assistant", "第一轮的回答")]
    summary = summarize(evicted, previous="更早的摘要", max_chars=300)
    assert "更早的摘要" in summary
    assert "用户: 第一轮的问题" in summary
    assert "助手: 第一轮的回答" in summary
    assert len(summary) <= 300


def test_summary_keeps_recent_when_over_limit() -> None:
    old = "旧" * 200
    evicted = [_turn("user", "最新的一句话")]
    summary = summarize(evicted, previous=old, max_chars=60)
    assert "最新的一句话" in summary, "超长时必须保留最近的"
    assert len(summary) <= 61  # 允许一个省略号


# ---------------------------------------------------------------------------
# L3 长期事实
# ---------------------------------------------------------------------------


def test_fact_dedup_and_limit() -> None:
    session = SessionState(session_id="s")
    assert remember_fact(session, " 我周三到周五不可用 ", limit=10) is True
    assert remember_fact(session, "我周三到周五不可用", limit=10) is False, "去空白后重复"

    for i in range(12):
        remember_fact(session, f"偏好 {i}", limit=10)
    assert len(session.facts) == 10
    assert session.facts[-1]["text"] == "偏好 11"


# ---------------------------------------------------------------------------
# 组合：compact / prompt 注入 / 落盘兼容
# ---------------------------------------------------------------------------


def test_compact_moves_evicted_turns_into_summary() -> None:
    session = SessionState(session_id="s")
    session.recent_turns = [
        _turn("user", _long("第一轮", 300)),
        _turn("assistant", _long("第二轮", 300)),
        _turn("user", "最新一轮"),
    ]
    stats = compact(session, MemoryConfig(window_tokens=50, max_turns=8, summary_chars=300))

    assert stats["evicted"] >= 1
    assert stats["kept"] >= 1
    assert session.summary, "被挤出的轮次要进摘要"
    assert session.archived_turns == stats["evicted"]
    assert session.recent_turns[-1].content == "最新一轮"


def test_context_blocks_order_and_content() -> None:
    session = SessionState(session_id="s", summary="之前的摘要")
    session.facts = [{"text": "我周三到周五不可用", "kind": "explicit"}]
    session.constraints = {"weekday_busy": True}

    blocks = context_blocks(session)
    assert blocks[0].startswith("【会话摘要】")
    assert blocks[1].startswith("【长期记忆】")
    assert "我周三到周五不可用" in blocks[1]
    assert blocks[2].startswith("【已确认的约束】")


def test_session_roundtrip_and_backward_compatible() -> None:
    session = SessionState(session_id="s", summary="摘要")
    session.facts = [{"text": "事实", "kind": "explicit"}]
    session.archived_turns = 4

    restored = SessionState.from_dict(session.to_dict())
    assert restored.summary == "摘要"
    assert restored.facts == session.facts
    assert restored.archived_turns == 4

    # 旧文件没有 facts/archived_turns 字段，也要能读
    legacy = SessionState.from_dict({"session_id": "old", "recent_turns": []})
    assert legacy.facts == []
    assert legacy.archived_turns == 0


def test_explicit_fact_parsing() -> None:
    from cadence.runtime.loop import _explicit_fact

    assert _explicit_fact("记住：我周三到周五不可用") == "我周三到周五不可用"
    assert _explicit_fact("请记住 我不吃辣") == "我不吃辣"
    assert _explicit_fact("记一下：周报周五交") == "周报周五交"
    assert _explicit_fact("我今天有什么任务") == ""
