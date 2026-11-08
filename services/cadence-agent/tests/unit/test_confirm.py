"""确认令牌：5 分钟过期 + 单次有效。

「单次有效」是最容易做错的一条：写成 peek 之后再 take，
用户连点两次确认就会执行两遍。
"""

from __future__ import annotations

from cadence.guard.confirm import ConfirmStore


def test_issue_and_take() -> None:
    store = ConfirmStore(ttl_s=300)
    pending = store.issue(tool="task.delete", args={"task_id": "t_001"}, preview={"kind": "delete"})
    assert pending.token.startswith("cf_")

    taken = store.take(pending.token)
    assert taken is not None
    assert taken.tool == "task.delete"


def test_take_is_single_use() -> None:
    store = ConfirmStore(ttl_s=300)
    token = store.issue(tool="task.delete", args={}).token
    assert store.take(token) is not None
    assert store.take(token) is None, "同一个令牌不能被用第二次"
    assert store.peek(token) is None


def test_expired_token_is_rejected() -> None:
    now = [1000.0]
    store = ConfirmStore(ttl_s=300, clock=lambda: now[0])
    token = store.issue(tool="task.delete", args={}).token

    now[0] += 299
    assert store.peek(token) is not None

    now[0] += 2
    assert store.peek(token) is None
    assert store.take(token) is None


def test_expires_in_counts_down() -> None:
    now = [1000.0]
    store = ConfirmStore(ttl_s=300, clock=lambda: now[0])
    pending = store.issue(tool="task.delete", args={})
    assert pending.expires_in() == 300
    now[0] += 120
    assert pending.expires_in() == 180


def test_discard_removes() -> None:
    store = ConfirmStore(ttl_s=300)
    token = store.issue(tool="task.delete", args={}).token
    assert store.discard(token) is True
    assert store.discard(token) is False
    assert len(store) == 0


def test_sweep_clears_expired() -> None:
    now = [1000.0]
    store = ConfirmStore(ttl_s=10, clock=lambda: now[0])
    store.issue(tool="task.delete", args={})
    store.issue(tool="task.delete", args={})
    now[0] += 11
    assert store.sweep() == 2
    assert len(store) == 0


def test_tokens_are_unguessable() -> None:
    store = ConfirmStore(ttl_s=300)
    tokens = {store.issue(tool="task.delete", args={}).token for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) > 16 for t in tokens)
