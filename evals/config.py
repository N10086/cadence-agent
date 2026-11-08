"""消融配置 B0–B3（本版只加载，不跑消融）。

冻结 spec 只要求 `load_configs(evals/configs)` 返回四个对象、顺序 B0..B3，
且 `configs[0].features.tool_contract is False`、`configs[-1].features.guard_id_whitelist is True`。
评测主流程固定用 B3；`compare` 不在 L4 范围。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Features:
    tool_contract: bool = False
    guard_id_whitelist: bool = False
    guard_confirm: bool = False
    clarify_enabled: bool = False
    model_routing: bool = False


@dataclass
class EvalConfig:
    name: str
    title: str = ""
    features: Features = field(default_factory=Features)
    raw: dict[str, Any] = field(default_factory=dict)


def load_configs(directory: str | Path) -> list[EvalConfig]:
    root = Path(directory)
    if not root.exists():
        raise FileNotFoundError(f"配置目录不存在：{root}")

    files = sorted(root.glob("*.yaml")) + sorted(root.glob("*.yml"))
    if not files:
        raise FileNotFoundError(f"配置目录里没有 YAML：{root}")

    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:  # pragma: no cover - 环境问题
        raise RuntimeError("读取消融配置需要 pyyaml（唯一允许的依赖）") from exc

    configs: list[EvalConfig] = []
    for file in files:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{file.name}: 配置顶层必须是对象")
        features_raw = raw.get("features") or {}
        if not isinstance(features_raw, dict):
            raise ValueError(f"{file.name}: features 必须是对象")
        configs.append(
            EvalConfig(
                name=str(raw.get("name") or file.stem).strip(),
                title=str(raw.get("title") or ""),
                features=Features(
                    tool_contract=bool(features_raw.get("tool_contract", False)),
                    guard_id_whitelist=bool(features_raw.get("guard_id_whitelist", False)),
                    guard_confirm=bool(features_raw.get("guard_confirm", False)),
                    clarify_enabled=bool(features_raw.get("clarify_enabled", False)),
                    model_routing=bool(features_raw.get("model_routing", False)),
                ),
                raw=raw,
            )
        )
    return configs


__all__ = ["EvalConfig", "Features", "load_configs"]
