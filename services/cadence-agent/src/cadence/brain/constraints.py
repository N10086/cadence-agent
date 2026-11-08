"""约束解析（SP2 桩：正则）。

SP3 会用规则 + 快模型把自然语言拆成结构化约束。
这里只实现最小可用子集：相对日期。

**时间基准必须注入，不能直接调 `now()`**（PRD 02 §3.3）：
不注入时钟，"下周三"在测试里就没法固定，40 条日期用例全部不可写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_WEEKDAYS = {
    "周一": 0, "周一": 0, "星期一": 0, "礼拜一": 0,
    "周二": 1, "星期二": 1, "礼拜二": 1,
    "周三": 2, "星期三": 2, "礼拜三": 2,
    "周四": 3, "星期四": 3, "礼拜四": 3,
    "周五": 4, "星期五": 4, "礼拜五": 4,
    "周六": 5, "星期六": 5, "礼拜六": 5,
    "周日": 6, "星期日": 6, "周天": 6, "星期天": 6, "礼拜天": 6,
}

_REL_DAYS = {
    "今天": 0,
    "today": 0,
    "明天": 1,
    "tomorrow": 1,
    "后天": 2,
    "大后天": 3,
    "昨天": -1,
}

_TIME_SUFFIX = re.compile(r"(\d{1,2})[:：点](\d{2})?")


@dataclass
class ParsedDate:
    raw: str
    value: datetime
    rule: str


def parse_relative(text: str, now: datetime) -> list[ParsedDate]:
    """从一句话里抠出相对日期。规则实现，必须 100% 正确（指标 date_parse_accuracy）。"""
    out: list[ParsedDate] = []
    msg = (text or "").strip()
    if not msg:
        return out

    for word, offset in _REL_DAYS.items():
        if word in msg:
            out.append(ParsedDate(word, _at_now(now) + timedelta(days=offset), f"relative:{word}"))

    for word, weekday in _WEEKDAYS.items():
        if word in msg:
            days = (weekday - now.weekday()) % 7
            if days == 0:
                days = 7  # "周三"说的是下一个周三，不是今天
            if "下" in msg:
                days += 7
            out.append(ParsedDate(word, _at_now(now) + timedelta(days=days), f"weekday:{word}"))

    match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", msg)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        year = now.year if month >= now.month else now.year + 1
        try:
            out.append(ParsedDate(match.group(0), now.replace(year=year, month=month, day=day), "absolute:md"))
        except ValueError:
            pass

    return out


def parse_time_of_day(text: str) -> tuple[int, int] | None:
    match = _TIME_SUFFIX.search(text or "")
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _at_now(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class NotSupported(Exception):
    """SP3 才有的能力。桩必须**显式失败**，不能假装成功。"""
