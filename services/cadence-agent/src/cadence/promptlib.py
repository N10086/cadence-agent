"""prompt 是代码，也要版本化。

每次 LLM 调用都把 `prompt_version` 写进 trace ——
这样"改 prompt 之后成功率是涨是跌"才有得比（SP4 的基线对比以此为前提）。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .config import PROMPTS_DIR

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, text[match.end() :]


@lru_cache(maxsize=32)
def load(name: str) -> tuple[str, str]:
    """返回 (正文, 版本号)。版本号缺失时退化为 'unversioned'（并在 trace 里看得出来）。"""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        return "", "unversioned"
    meta, body = _split_front_matter(path.read_text(encoding="utf-8"))
    return body.strip(), meta.get("version", "unversioned")


def system_prompt(tier: str) -> tuple[str, str]:
    name = "system.fast" if tier == "fast" else "system"
    body, version = load(name)
    if not body:
        body, version = load("system")
    return body, version


def render(name: str, **kwargs: object) -> str:
    body, _ = load(name)
    for key, value in kwargs.items():
        body = body.replace("{" + key + "}", str(value))
    return body


def prompt_dir() -> Path:
    return PROMPTS_DIR
