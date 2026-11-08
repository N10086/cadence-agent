"""指纹去重：相同 (tool, args) 连续两次 = 卡死。"""

from __future__ import annotations

from cadence.runtime.fingerprint import FingerprintTracker, fingerprint


def test_same_call_twice_is_stuck() -> None:
    tracker = FingerprintTracker()
    args = {"keyword": "牛奶"}
    assert tracker.is_stuck("task.search", args)[0] is False
    assert tracker.is_stuck("task.search", args)[0] is True


def test_different_args_not_stuck() -> None:
    tracker = FingerprintTracker()
    assert tracker.is_stuck("task.search", {"keyword": "牛奶"})[0] is False
    assert tracker.is_stuck("task.search", {"keyword": "周报"})[0] is False


def test_different_tools_not_stuck() -> None:
    tracker = FingerprintTracker()
    assert tracker.is_stuck("task.search", {"task_id": "t_001"})[0] is False
    assert tracker.is_stuck("task.complete", {"task_id": "t_001"})[0] is False


def test_key_order_does_not_matter() -> None:
    """模型两次给的参数键序可能不同，但它们是同一次调用。"""
    a = {"keyword": "牛奶", "status": "todo"}
    b = {"status": "todo", "keyword": "牛奶"}
    assert fingerprint("task.search", a) == fingerprint("task.search", b)


def test_reset_clears() -> None:
    tracker = FingerprintTracker()
    tracker.is_stuck("task.search", {"keyword": "牛奶"})
    tracker.reset()
    assert tracker.is_stuck("task.search", {"keyword": "牛奶"})[0] is False


def test_alternating_is_not_stuck() -> None:
    """A B A B 不是卡死：真正要抓的是紧邻重复。"""
    tracker = FingerprintTracker()
    assert tracker.is_stuck("task.search", {"keyword": "a"})[0] is False
    assert tracker.is_stuck("task.search", {"keyword": "b"})[0] is False
    assert tracker.is_stuck("task.search", {"keyword": "a"})[0] is False
