"""工具调用指纹去重。

规则（FR-3）：**相同 `(tool, args)` 连续出现 2 次，即判定卡死。**

为什么要按 args 排序序列化：模型两次调 `task.search(keyword=..., status=...)`
参数键顺序可能不同，但它们是同一次调用。不归一化就会漏判。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..llm.base import ToolCall


def fingerprint(tool: str, args: dict) -> str:
    payload = ToolCall(name=tool, arguments=args).fingerprint_payload()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class FingerprintTracker:
    """只记「上一次」，不记全集。

    记全集会把"先 search 再 search 同样的词"误判成卡死——
    真正要抓的是**紧邻重复**：模型卡在同一个动作上反复试。
    """

    last: str | None = None
    repeats: int = 0

    def observe(self, tool: str, args: dict) -> str:
        return fingerprint(tool, args)

    def is_stuck(self, tool: str, args: dict) -> tuple[bool, str]:
        """返回 (是否卡死, 指纹)。"""
        fp = fingerprint(tool, args)
        if fp == self.last:
            self.repeats += 1
            return True, fp
        self.last = fp
        self.repeats = 0
        return False, fp

    def reset(self) -> None:
        self.last = None
        self.repeats = 0
