"""步数 / token / 成本预算。

三档都能独立触发中止。`BUDGET_EXCEEDED` 不可恢复 —— 这是防止"烧钱"的闸。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .errors import BudgetExceeded

_NANO_PER_YUAN = 1_000_000_000


def to_nano(yuan: float) -> int:
    """元 → 整数纳元（四舍五入，**half-up**）。

    两个坑都要避开：
    1. 不能直接 `float +=`：二进制浮点无法精确表示 0.1，累加会漂；
    2. 不能 `round(yuan * 1e9)`：`round` 是银行家舍入（.5 向偶数），且
       `0.015877 * 1e9` 本身可能是 `15876999.999999998`。
    所以先用 `Decimal(str(yuan))` 拿到十进制字面量，再乘、再 half-up。
    """
    value = Decimal(str(yuan)) * _NANO_PER_YUAN
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


@dataclass
class Budget:
    max_steps: int = 8
    max_tokens: int = 4000
    max_cost_yuan: float | None = None

    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    #: 成本用**整数纳元**累加（1 元 = 1e9 纳元）。展示/JSON 边界才除回元。
    cost_nano: int = 0
    history: list[str] = field(default_factory=list)

    @property
    def cost_yuan(self) -> float:
        """只用于展示与外部接口；内部累加永远走 `cost_nano`。"""
        return self.cost_nano / _NANO_PER_YUAN

    # ------------------------------------------------------------------

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def steps_left(self) -> int:
        return max(0, self.max_steps - self.steps)

    def can_start_step(self) -> bool:
        return self.steps < self.max_steps

    def start_step(self) -> int:
        """每进入一次 ACT 记一步。超预算抛错（调用方负责把已完成的部分汇报出去）。"""
        if self.steps >= self.max_steps:
            raise BudgetExceeded(
                f"步数预算用尽（{self.max_steps} 步），已执行的动作不回滚",
                details={"steps": self.steps, "max_steps": self.max_steps, "history": self.history},
            )
        self.steps += 1
        return self.steps

    def note(self, action: str) -> None:
        self.history.append(action)

    def add_usage(self, *, tokens_in: int, tokens_out: int, cost_yuan: float) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_nano += to_nano(cost_yuan)

        if self.tokens_total > self.max_tokens:
            raise BudgetExceeded(
                f"token 预算用尽（{self.tokens_total} > {self.max_tokens}）",
                details={"tokens_in": self.tokens_in, "tokens_out": self.tokens_out},
            )
        max_nano = to_nano(self.max_cost_yuan) if self.max_cost_yuan is not None else None
        if max_nano is not None and self.cost_nano > max_nano:
            raise BudgetExceeded(
                f"成本预算用尽（¥{self.cost_yuan:.4f} > ¥{self.max_cost_yuan:.4f}）",
                details={"cost_nano": self.cost_nano, "cost_yuan": self.cost_yuan},
            )

    def snapshot(self) -> dict[str, object]:
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_total": self.tokens_total,
            "cost_nano": self.cost_nano,
            "cost_yuan": round(self.cost_yuan, 6),
        }
