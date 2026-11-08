"""铁律的静态守卫：资源图只允许单向边，且只有一把可等待锁。

这些断言不看行为、只看源码结构。理由：死锁 bug 极难复现，等它在压测里出现
已经太晚；而"多了一把锁/多了一个反向等待"是可以在 CI 里 1 毫秒发现的结构问题。

四条：
1. `asyncio.Lock(` 只允许出现在 `runtime/session.py`（会话锁，全进程唯一）。
2. `asyncio.Semaphore(` 只允许出现在 `deps.py`（全局 LLM 信号量，叶子）。
3. 不许出现 `asyncio.Condition(`（流缓冲已改成无锁 Event 队列），不许 import threading。
4. `except ...CancelledError` 的 handler 里必须有 `raise`（取消必须上抛）。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "cadence"


def _python_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def _read(path: Path) -> str:
    # 仓库里有文件带 UTF-8 BOM；ast.parse 不吃 BOM，统一用 utf-8-sig
    return path.read_text(encoding="utf-8-sig")


def _calls_of(name: str) -> list[str]:
    """找出 `asyncio.<name>(...)` 出现在哪些文件。"""
    hits: list[str] = []
    for path in _python_files():
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == name
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "asyncio"
            ):
                hits.append(path.relative_to(SRC).as_posix())
    return sorted(set(hits))


def test_only_one_waitable_lock_in_the_process() -> None:
    assert _calls_of("Lock") == ["runtime/session.py"], (
        "全进程只允许会话锁一把 asyncio.Lock；新增锁等于给资源图加边"
    )


def test_llm_semaphore_is_created_once_in_deps() -> None:
    assert _calls_of("Semaphore") == ["deps.py"], (
        "LLM 信号量只允许在 deps.py 装配一次；散布出去就无法保证它是叶子"
    )


def test_no_condition_and_no_threading() -> None:
    assert _calls_of("Condition") == [], "流缓冲必须是无锁单写者队列，不许用 Condition"
    for path in _python_files():
        source = _read(path)
        assert "import threading" not in source, f"{path} 引入了 threading"


def test_cancelled_error_is_always_re_raised() -> None:
    for path in _python_files():
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            names = _handler_type_names(node.type)
            if "CancelledError" not in names:
                continue
            body_has_raise = any(isinstance(stmt, ast.Raise) for stmt in node.body)
            assert body_has_raise, (
                f"{path.relative_to(SRC)} 里 except CancelledError 没有 raise —— 取消被吞了"
            )


def _handler_type_names(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        out: set[str] = set()
        for elt in node.elts:
            out |= _handler_type_names(elt)
        return out
    return set()


def test_deps_documents_the_single_direction_edge() -> None:
    """注释不是装饰：这条文档是"为什么不会死锁"的唯一书面依据，删掉就该挂测试。"""
    text = (SRC / "deps.py").read_text(encoding="utf-8")
    assert "会话锁" in text and "LLM 信号量" in text
    assert "单向边" in text


def test_emit_does_not_take_a_second_lock() -> None:
    """SSE append 在会话锁内被调用，所以它自己不能再取锁。"""
    from cadence.api import sse as sse_mod

    source = _read(Path(sse_mod.__file__))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"Condition", "Lock", "Semaphore"}, (
                "流缓冲里不许出现可等待锁"
            )
    assert "_wake" in source  # 用 Event 唤醒 tail
