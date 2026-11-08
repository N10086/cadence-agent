"""SP3 当前状态的测试。

SP3（cadence-brain）在仓库里只有挂载点：
- `intent.py` 是 6 类关键词规则（不是 PRD 的 7 类）；
- `constraints.py` 只支持最小相对日期；
- `decompose.py` 显式抛 NotSupported；
- `proactive.py` 返回空列表；
- `solver/__init__.py` 是空文件。

本文件测的是"当前桩是否诚实"（显式失败 / 哨兵 / 不假装成功），
以及 PRD 要求但尚未实现的日期语义（用 xfail 标记）。
真正的求解器/分解验收在 `test_sp3_acceptance_spec.py`，默认 skip。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cadence.brain import decompose, intent, proactive
from cadence.brain.constraints import NotSupported, parse_relative

TZ8 = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 8, 10, 30, tzinfo=TZ8)


# ---------------------------------------------------------------------
# 桩必须"显式失败"，不能返回看起来合理的结果
# ---------------------------------------------------------------------


async def test_decompose_stub_raises_not_supported() -> None:
    with pytest.raises(NotSupported) as ei:
        await decompose.decompose("准备下周的面试", now=NOW)
    assert "SP3" in str(ei.value)


async def test_proactive_stub_returns_empty_with_sentinel() -> None:
    assert proactive.SP2_IMPLEMENTED is False
    assert await proactive.suggest(tasks=[], now=NOW) == []


def test_solver_is_currently_an_empty_stub() -> None:
    import cadence.brain.solver as solver

    assert not hasattr(solver, "solve"), (
        "SP3 已开始实现求解器：请删除本断言，并把 test_sp3_acceptance_spec.py 的 skip 打开"
    )


# ---------------------------------------------------------------------
# 意图：PRD 要 7 类，当前只有 6 类
# ---------------------------------------------------------------------


def test_current_intent_enum_is_six_classes() -> None:
    assert {i.value for i in intent.Intent} == {
        "query", "mutate", "multi", "destructive", "chitchat", "unknown",
    }


@pytest.mark.xfail(strict=True, reason="SP3 FR-1 要求 DECOMPOSE / RESCHEDULE 两类，当前 intent.py 没有")
def test_intent_has_seven_classes() -> None:
    assert {"decompose", "reschedule"} <= {i.value for i in intent.Intent}


# ---------------------------------------------------------------------
# 时间解析：当前只支持最小子集
# ---------------------------------------------------------------------


def test_supported_relative_days_still_work() -> None:
    got = parse_relative("今天", NOW)
    assert len(got) == 1
    assert got[0].value.date().isoformat() == "2026-11-08"


@pytest.mark.parametrize("text", ["三天后", "3天后", "月底", "下个月初"])
def test_current_parser_misses_sp3_fr2_expressions(text: str) -> None:
    """锁定当前行为：这些 PRD 明确要求的表达式当前返回空。"""
    assert parse_relative(text, NOW) == []


@pytest.mark.xfail(strict=True, reason="SP3 FR-2 要求支持'三天后/3天后'，当前 constraints.py 未实现")
@pytest.mark.parametrize("text,days", [("三天后", 3), ("3天后", 3)])
def test_three_days_later_is_supported(text: str, days: int) -> None:
    got = parse_relative(text, NOW)
    assert got
    assert got[0].value.date().isoformat() == (NOW + timedelta(days=days)).date().isoformat()


@pytest.mark.xfail(strict=True, reason="SP3 FR-2 要求'月底'=当月最后一天 23:59，当前未实现")
def test_end_of_month_is_supported() -> None:
    got = parse_relative("月底", NOW)
    assert got
    assert got[0].value.day == 30  # 2026-11 的最后一天
    assert (got[0].value.hour, got[0].value.minute) == (23, 59)


@pytest.mark.xfail(strict=True, reason="SP3 FR-2 要求'下个月初'，当前未实现")
def test_next_month_start_is_supported() -> None:
    got = parse_relative("下个月初", NOW)
    assert got
    assert got[0].value.date().isoformat() == "2026-12-01"
