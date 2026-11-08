"""evals 测试的路径引导。

`pytest evals/tests` 的 console script 不一定把仓库根放进 sys.path
（`python -m pytest` 会，但用户敲的是 `pytest`）。这里显式补上，保证
`import evals` 在任何调用方式下都成立。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
