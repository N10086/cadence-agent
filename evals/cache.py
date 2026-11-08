"""结果缓存（L4-6）。

接口冻结为 `RunCache.put/get(model, prompt_hash, input, output)`：

    key = sha256(model | prompt_hash | input)

默认落盘到 `evals/.cache/run_cache.json`（已 gitignore）。缓存的是**一次用例的
CaseResult**，不是 LLM 的逐 token 输出 —— 评测关心的是"同一输入 + 同一模型 + 同一
prompt 是否得到同一结论"，这比缓存半截流式响应更稳，也不会把工具副作用重放一遍。

口径：命中率 = hits / (hits + misses)，报告头必须写。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_DEFAULT_PATH = Path(__file__).resolve().parent / ".cache" / "run_cache.json"


def _digest(model: str, prompt_hash: str, input: str) -> str:
    payload = f"{model}\x1f{prompt_hash}\x1f{input}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class RunCache:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else _DEFAULT_PATH
        self.hits = 0
        self.misses = 0
        self._data: dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(payload, dict):
            self._data = payload

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=0), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            # 缓存写不进去不影响评测结果，最多下次少省一点钱。
            pass

    # ------------------------------------------------------------------

    def put(self, *, model: str, prompt_hash: str, input: str, output: Any) -> None:
        self._data[_digest(model, prompt_hash, input)] = {
            "model": model,
            "prompt_hash": prompt_hash,
            "input": input,
            "output": output,
        }
        self._save()

    def get(self, *, model: str, prompt_hash: str, input: str) -> Any | None:
        key = _digest(model, prompt_hash, input)
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        return entry.get("output")

    # ------------------------------------------------------------------

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0

    def clear(self) -> None:
        self._data.clear()
        self.hits = 0
        self.misses = 0
        self._save()

    def __len__(self) -> int:
        return len(self._data)


__all__ = ["RunCache"]
