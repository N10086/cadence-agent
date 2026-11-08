"""步数 / token / 成本预算。

三档都能独立触发中止。`BUDGET_EXCEEDED` 不可恢复 —— 这是防止"烧钱"的闸。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import BudgetExceeded


@dataclass
class Budget:
    max_steps: int = 8
    max_tokens: int = 4000
    max_cost_yuan: float | None = None

    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_yuan: float = 0.0
    history: list[str] = field(default_factory=list)

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
        self.cost_yuan += cost_yuan

        if self.tokens_total > self.max_tokens:
            raise BudgetExceeded(
                f"token 预算用尽（{self.tokens_total} > {self.max_tokens}）",
                details={"tokens_in": self.tokens_in, "tokens_out": self.tokens_out},
            )
        if self.max_cost_yuan is not None and self.cost_yuan > self.max_cost_yuan:
            raise BudgetExceeded(
                f"成本预算用尽（¥{self.cost_yuan:.4f} > ¥{self.max_cost_yuan:.4f}）",
                details={"cost_yuan": self.cost_yuan},
            )

    def snapshot(self) -> dict[str, object]:
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_total": self.tokens_total,
            "cost_yuan": round(self.cost_yuan, 6),
        }
