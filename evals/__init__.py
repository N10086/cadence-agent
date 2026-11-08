"""Cadence 评测包（L4）。

`evals` 与 `services/` 完全解耦：运行时只通过 HTTP / 公开 Python 接口调用，
不 import Core 的 Java 类，也不把评测代码写进生产路径。

唯一的例外是 `--core fake` 时复用 Agent 测试夹具 `tests/fixtures/fake_core.py`
（显式要求：不新写第二套假 Core）。这个路径由 `bootstrap` 在导入时加进 sys.path。
"""

from __future__ import annotations

from .bootstrap import REPO_ROOT, SERVICE_DIR, ensure_paths

ensure_paths()

__all__ = ["REPO_ROOT", "SERVICE_DIR"]
