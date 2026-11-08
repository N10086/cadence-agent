"""让 `python -m evals.cli` 在仓库根目录也能 import 到 `cadence`。

不装 editable 包、不依赖 pytest 的 `pythonpath` 配置：
评测器本来就要能在任意 cwd 下跑，路径必须自己负责。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = REPO_ROOT / "services" / "cadence-agent"

_PATHS = (SERVICE_DIR / "src", SERVICE_DIR / "tests")


def ensure_paths() -> None:
    for path in reversed(_PATHS):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)


__all__ = ["REPO_ROOT", "SERVICE_DIR", "ensure_paths"]
