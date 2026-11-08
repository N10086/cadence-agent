"""预算：三档（步数 / token / 成本）都必须能中止。"""

from __future__ import annotations

import pytest

from cadence.runtime.budget import Budget
from cadence.runtime.errors import BudgetExceeded, ErrorCode


def test_steps_count_up() -> None:
    budget = Budget(max_steps=3)
    assert budget.start_step() == 1
    assert budget.start_step() == 2
    assert budget.start_step() == 3
    assert budget.steps_left == 0
    assert not budget.can_start_step()


def test_exceeding_steps_raises_non_recoverable() -> None:
    budget = Budget(max_steps=2)
    budget.start_step()
    budget.start_step()
    with pytest.raises(BudgetExceeded) as exc:
        budget.start_step()
    assert exc.value.code is ErrorCode.BUDGET_EXCEEDED
    assert exc.value.recoverable is False


def test_token_budget() -> None:
    budget = Budget(max_steps=8, max_tokens=1000)
    budget.add_usage(tokens_in=400, tokens_out=100, cost_yuan=0.0)
    assert budget.tokens_total == 500
    with pytest.raises(BudgetExceeded):
        budget.add_usage(tokens_in=400, tokens_out=200, cost_yuan=0.0)


def test_cost_budget() -> None:
    budget = Budget(max_steps=8, max_tokens=10**9, max_cost_yuan=0.01)
    budget.add_usage(tokens_in=10, tokens_out=10, cost_yuan=0.006)
    with pytest.raises(BudgetExceeded):
        budget.add_usage(tokens_in=10, tokens_out=10, cost_yuan=0.006)


def test_history_records_actions() -> None:
    budget = Budget()
    budget.note("创建任务「买牛奶」")
    budget.note("完成任务「写周报」")
    assert budget.snapshot()["steps"] == 0
    assert len(budget.history) == 2
