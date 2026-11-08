"""prompt 加载。

这条测试存在的理由：曾经因为 PROMPTS_DIR 指到 `src/prompts` 而不是
`src/cadence/prompts`，系统 prompt **静默变成空字符串** ——
模型完全没有指令，表现为疯狂重复调工具。
一个 assert 就能防住这类事故。
"""

from __future__ import annotations

from cadence import promptlib
from cadence.config import PROMPTS_DIR


def test_prompts_dir_points_into_the_package() -> None:
    assert PROMPTS_DIR.exists()
    assert PROMPTS_DIR.name == "prompts"
    assert (PROMPTS_DIR / "system.md").exists()
    assert (PROMPTS_DIR / "system.fast.md").exists()


def test_system_prompt_is_not_empty() -> None:
    for tier in ("fast", "reason"):
        body, version = promptlib.system_prompt(tier)
        assert len(body) > 100, f"{tier} 的 system prompt 几乎是空的"
        assert version != "unversioned", f"{tier} 的 prompt 缺 front-matter 版本号"


def test_both_tiers_differ() -> None:
    fast, _ = promptlib.system_prompt("fast")
    reason, _ = promptlib.system_prompt("reason")
    assert fast != reason, "两个 tier 应该用不同长度的 prompt"


def test_hard_rules_are_present() -> None:
    """五条硬规则各自对应一个评测分组，缺一条就会少测一组。"""
    body, _ = promptlib.system_prompt("reason")
    assert "task.delete" in body
    assert "task.search" in body
    assert "meta.clarify" in body
    assert "data" in body.lower() or "数据" in body
    assert "actions_taken" in body
