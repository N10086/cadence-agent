"""token → 成本换算。

定价来自 config（元 / 百万 token），**不要在代码里硬编码价格**：
价格会变，而成本是评测指标之一（NFR-3 单轮 ≤ ¥0.01），写死了就会悄悄失真。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings


@dataclass(frozen=True)
class Pricing:
    in_per_m: float
    out_per_m: float

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in / 1_000_000) * self.in_per_m + (tokens_out / 1_000_000) * self.out_per_m


def pricing_for(settings: Settings, model: str) -> Pricing:
    m = model.lower()
    if "fake" in m:
        return Pricing(0.0, 0.0)  # 假模型不花钱，成本指标不能把它算进去
    if "glm" in m or "zhipu" in m or "bigmodel" in m:
        return Pricing(settings.zhipu_price_per_m, settings.zhipu_price_per_m)
    return Pricing(settings.deepseek_price_in_per_m, settings.deepseek_price_out_per_m)


def cost_of(settings: Settings, model: str, tokens_in: int, tokens_out: int) -> float:
    return pricing_for(settings, model).cost(tokens_in, tokens_out)


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（无 tiktoken 依赖）。

    经验值：中文约 1 token / 1.5 字，英文约 1 token / 4 字符。
    只用于**预算兜底**与上下文压缩触发；真实成本一律以 provider 返回的 usage 为准。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4) + 1
