"""时间解析、意图分类、Core 客户端与 LLM 客户端的边界测试。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from cadence.brain import intent
from cadence.brain.constraints import parse_relative, parse_time_of_day
from cadence.config import Settings
from cadence.llm.base import LLMBadOutput, LLMRateLimited
from cadence.llm.deepseek import DeepSeekClient
from cadence.llm.retry import Breaker, CLOSED, HALF_OPEN, OPEN
from cadence.runtime.errors import ErrorCode, ToolError
from cadence.tools.core_client import CoreClient, classify_422

TZ8 = timezone(timedelta(hours=8))
WED = datetime(2026, 11, 4, 10, 0, tzinfo=TZ8)      # 周三
SUN = datetime(2026, 11, 8, 10, 0, tzinfo=TZ8)      # 周日
SAT = datetime(2026, 11, 7, 10, 0, tzinfo=TZ8)      # 周六


# =====================================================================
# 相对日期
# =====================================================================


@pytest.mark.xfail(strict=True, reason="BUG-SP2-19：任何含'下'的句子都会额外 +7 天；"
                                       "周日说'下周三'被算成 10 天后（11-18），而不是 3 天后（11-11）")
def test_next_wednesday_from_sunday() -> None:
    got = parse_relative("下周三", SUN)
    assert len(got) == 1
    # 周日说"下周三"，中文语义是 3 天后的周三（11-11），不是 10 天后
    assert got[0].value.date().isoformat() == "2026-11-11"


def test_bug_next_wednesday_from_sunday_is_ten_days_later() -> None:
    got = parse_relative("下周三", SUN)
    assert got[0].value.date().isoformat() == "2026-11-18"


@pytest.mark.xfail(strict=True, reason="BUG-SP2-19：today==目标星期时先 +7 再因'下'再 +7，"
                                       "'下周三'在周三说会算成 14 天后")
def test_next_wednesday_on_wednesday() -> None:
    got = parse_relative("下周三", WED)
    assert got[0].value.date().isoformat() == "2026-11-11"


def test_bug_next_wednesday_on_wednesday_is_two_weeks_later() -> None:
    got = parse_relative("下周三", WED)
    assert got[0].value.date().isoformat() == "2026-11-18"


def test_this_friday_on_saturday_should_be_past() -> None:
    """BUG-SP2-19：'本周五'在周六说，实现算成下周五。"""
    got = parse_relative("本周五", SAT)
    assert got[0].value.date().isoformat() == "2026-11-13"  # 当前错误行为


@pytest.mark.xfail(strict=True, reason="BUG-SP2-19：'本周X'没有过去/未来方向判断，"
                                       "在周六说'本周五'会算到下周")
def test_bug_this_friday_on_saturday_is_past() -> None:
    got = parse_relative("本周五", SAT)
    assert got[0].value.date().isoformat() == "2026-11-06"


def test_absolute_month_day_rolls_to_next_year() -> None:
    got = parse_relative("3月5日", SUN)
    assert got[0].value.date().isoformat() == "2027-03-05"


def test_invalid_absolute_date_is_skipped() -> None:
    assert parse_relative("2月30日", SUN) == []


def test_relative_day_and_weekday_can_both_match() -> None:
    got = parse_relative("今天周三", SUN)
    assert len(got) == 2


@pytest.mark.parametrize("text,expected", [
    ("9:30", (9, 30)),
    ("9点", (9, 0)),
    ("23:59", (23, 59)),
    ("24:00", None),
    ("12:60", None),
    ("没有时间", None),
])
def test_parse_time_of_day(text: str, expected) -> None:
    assert parse_time_of_day(text) == expected


# =====================================================================
# 意图分类
# =====================================================================


def test_destructive_vague_request_should_clarify() -> None:
    """BUG-SP2-18：DESTRUCTIVE 分支在歧义检查之前，'删除那个'不会触发 clarify。"""
    result = intent.classify("删除那个")
    assert result.intent is intent.Intent.DESTRUCTIVE
    assert result.ambiguous is False


@pytest.mark.xfail(strict=True, reason="BUG-SP2-18：破坏性意图也要先过歧义检测，"
                                       "'删除那个'应 clarify 而不是直接交给模型猜")
def test_bug_destructive_vague_should_be_ambiguous() -> None:
    assert intent.classify("删除那个").ambiguous is True


def test_chitchat_with_request_is_misclassified() -> None:
    result = intent.classify("你好，帮我建个任务")
    assert result.intent is intent.Intent.CHITCHAT


@pytest.mark.xfail(strict=True, reason="BUG-SP2-18：CHITCHAT 用 ^ 前缀匹配，"
                                       "'你好，帮我建个任务'被当成纯寒暄，整轮不调工具")
def test_bug_chitchat_with_request_should_be_mutate() -> None:
    assert intent.classify("你好，帮我建个任务").intent is intent.Intent.MUTATE


def test_plain_query_and_mutate() -> None:
    assert intent.classify("看看我今天有什么任务").intent is intent.Intent.QUERY
    assert intent.classify("把买牛奶标记完成").intent is intent.Intent.MUTATE
    assert intent.classify("完成任务然后建一个报告").intent is intent.Intent.MULTI


# =====================================================================
# CoreClient 错误映射
# =====================================================================


def _settings(**kw) -> Settings:
    base = dict(llm_mode="fake", deepseek_api_key="", core_retry_times=0, core_retry_backoff_s=0.0)
    base.update(kw)
    return Settings(**base)


def _client(handler, settings=None) -> CoreClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="http://core.test", transport=transport)
    return CoreClient(settings or _settings(), http=http, sleep=lambda _s: _noop())


async def _noop() -> None:
    return None


async def test_403_without_token_is_confirm_required() -> None:
    def handler(request):
        return httpx.Response(403, json={"code": 403, "message": "需要确认"})

    with pytest.raises(ToolError) as ei:
        await _client(handler).request("DELETE", "/api/v1/tasks/t_1")
    assert ei.value.code is ErrorCode.CONFIRM_REQUIRED
    assert (ei.value.details or {}).get("confirmToken") is None


async def test_200_with_nonzero_code_is_upstream_error() -> None:
    def handler(request):
        return httpx.Response(200, json={"code": 500, "message": "oops"})

    with pytest.raises(ToolError) as ei:
        await _client(handler).request("GET", "/api/v1/tasks")
    assert ei.value.code is ErrorCode.UPSTREAM_UNAVAILABLE


async def test_200_empty_body_returns_none_current_behavior() -> None:
    """BUG-SP2-37：200 空 body 被当成成功（204 语义），调用方会拿到 None。"""
    def handler(request):
        return httpx.Response(200)

    assert await _client(handler).request("GET", "/api/v1/tasks") is None


async def test_429_is_rate_limited_without_retry() -> None:
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(429, json={"code": 429, "message": "slow"})

    with pytest.raises(ToolError) as ei:
        await _client(handler).request("GET", "/api/v1/tasks")
    assert ei.value.code is ErrorCode.RATE_LIMITED
    assert attempts["n"] == 1, "Core 的 4xx 不重试"


async def test_timeout_retries_then_raises() -> None:
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ToolError) as ei:
        await _client(handler, _settings(core_retry_times=2)).request("GET", "/api/v1/tasks")
    assert ei.value.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert attempts["n"] == 3


def test_422_complete_cancelled_is_misclassified_as_dependency() -> None:
    """BUG-SP2-16：Core 的 422 无符号码，complete 的'已取消'被兜底成依赖错误。"""
    assert classify_422("已取消的任务不能标记完成", "task.complete") is ErrorCode.DEPENDENCY_VIOLATION


@pytest.mark.xfail(strict=True, reason="BUG-SP2-16：'已取消的任务不能标记完成'是 ILLEGAL_TRANSITION，"
                                       "不是依赖未完成")
def test_bug_422_cancelled_complete_is_illegal_transition() -> None:
    assert classify_422("已取消的任务不能标记完成", "task.complete") is ErrorCode.ILLEGAL_TRANSITION


# =====================================================================
# DeepSeek 流式边界
# =====================================================================


def _deepseek(handler) -> DeepSeekClient:
    http = httpx.AsyncClient(base_url="https://api.deepseek.com/v1", transport=httpx.MockTransport(handler))
    return DeepSeekClient(api_key="sk-test", base_url="https://api.deepseek.com/v1",
                          model="deepseek-chat", http=http)


def _sse(*chunks: dict, done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode()


def _delta(content: str | None = None, tool_calls: list[dict] | None = None,
           finish: str | None = None) -> dict:
    d: dict = {}
    if content is not None:
        d["content"] = content
    if tool_calls is not None:
        d["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "delta": d, "finish_reason": finish}], "model": "deepseek-chat"}


async def _collect(client: DeepSeekClient) -> list:
    return [ev async for ev in client.stream([{"role": "user", "content": "hi"}])]


async def test_truncated_stream_with_partial_text_is_accepted_current_behavior() -> None:
    """BUG-SP2-26：没有 [DONE] 但已有部分文本时，实现当成正常结束。"""
    def handler(request):
        return httpx.Response(200, content=_sse(_delta("半截回答"), done=False))

    events = await _collect(_deepseek(handler))
    assert events[-1].kind == "done"
    assert events[-1].response.text == "半截回答"


@pytest.mark.xfail(strict=True, reason="BUG-SP2-26：流在 [DONE] 之前断开必须抛 STREAM_INTERRUPTED，"
                                       "半截文本不能当成完整回答")
async def test_bug_truncated_stream_with_partial_text_is_rejected() -> None:
    def handler(request):
        return httpx.Response(200, content=_sse(_delta("半截回答"), done=False))

    with pytest.raises(LLMBadOutput):
        await _collect(_deepseek(handler))


async def test_empty_stream_returns_empty_answer_current_behavior() -> None:
    def handler(request):
        return httpx.Response(200, content=b"")

    events = await _collect(_deepseek(handler))
    assert events[-1].response.text == ""


async def test_tool_call_without_name_is_rejected() -> None:
    def handler(request):
        return httpx.Response(200, content=_sse(
            _delta(tool_calls=[{"index": 0, "id": "c1", "function": {"arguments": "{}"}}], finish="tool_calls"),
        ))

    with pytest.raises(LLMBadOutput):
        await _collect(_deepseek(handler))


async def test_retry_after_http_date_crashes_current_behavior() -> None:
    """BUG-SP2-26：Retry-After 允许 HTTP-date，float() 会抛 ValueError。"""
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
                              json={"error": "slow"})

    with pytest.raises(ValueError):
        await _deepseek(handler).chat([{"role": "user", "content": "hi"}])


@pytest.mark.xfail(strict=True, reason="BUG-SP2-26：HTTP-date 形式的 Retry-After 应被解析，"
                                       "而不是抛 ValueError")
async def test_bug_retry_after_http_date_is_parsed() -> None:
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
                              json={"error": "slow"})

    with pytest.raises(LLMRateLimited):
        await _deepseek(handler).chat([{"role": "user", "content": "hi"}])


# =====================================================================
# 熔断器
# =====================================================================


def test_breaker_opens_after_four_failures() -> None:
    b = Breaker("t", failure_threshold=0.5, window=20)
    for _ in range(3):
        b.record(False)
    assert b.state == CLOSED
    b.record(False)
    assert b.state == OPEN


def test_breaker_first_open_cooldown_is_30s() -> None:
    """L2-1：首次打开就是 30s。旧实现首次即翻倍成 60s（BUG-SP2-25）。"""
    clock = [0.0]
    b = Breaker("t", failure_threshold=0.5, window=20, cooldown_s=30, clock=lambda: clock[0])
    for _ in range(4):
        b.record(False)
    assert b.state == OPEN
    clock[0] = 29.9
    assert b.state == OPEN  # 冷却未满，仍然不打 provider
    clock[0] = 30.0
    assert b.state == HALF_OPEN


def test_breaker_state_machine_uses_injected_clock() -> None:
    """CLOSED → OPEN → HALF_OPEN →（探测成功）CLOSED，全程注入时钟，不 sleep。

    这是 L2-1 的核心验收：状态机必须能用假时钟走完，否则要么真的睡 30s，
    要么测试只能断言"打开过"，覆盖不到半开窗口和恢复路径。
    """
    clock = [0.0]
    b = Breaker("t", failure_threshold=0.5, window=4, cooldown_s=30, clock=lambda: clock[0])
    assert b.state == CLOSED

    for _ in range(4):
        b.record(False)
    assert b.state == OPEN
    assert b.allow() is False

    clock[0] = 30.0
    assert b.state == HALF_OPEN
    assert b.allow() is True

    b.record(True)  # 探测成功
    assert b.state == CLOSED
    assert b.open_count == 0


def test_breaker_cooldown_backs_off_after_first_open() -> None:
    """首次 30s，之后才 ×2：60 → 120 → 240 → 300（封顶）。"""
    clock = [0.0]
    b = Breaker("t", failure_threshold=0.5, window=4, cooldown_s=30, cooldown_max_s=300,
                clock=lambda: clock[0])

    seen: list[float] = []
    for _ in range(5):
        for _ in range(4):
            b.record(False)
        assert b.state == OPEN
        seen.append(b.cooldown_current)
        # 时钟以"本次打开时刻"为基准推进：不能累加 now，否则后面的档位永远走不到。
        clock[0] += b.cooldown_current + 0.1
        assert b.state == HALF_OPEN
        b.record(False)  # 探测失败 → 再次打开，进入下一档

    assert seen == [30.0, 60.0, 120.0, 240.0, 300.0]


def test_breaker_half_open_success_resets() -> None:
    clock = [0.0]
    b = Breaker("t", failure_threshold=0.5, window=20, cooldown_s=30, clock=lambda: clock[0])
    for _ in range(4):
        b.record(False)
    clock[0] = 30.0
    assert b.state == HALF_OPEN
    b.record(True)
    assert b.state == CLOSED


def test_breaker_guard_raises_when_open() -> None:
    b = Breaker("t", failure_threshold=0.5, window=20)
    for _ in range(4):
        b.record(False)
    from cadence.llm.base import LLMBreakerOpen
    with pytest.raises(LLMBreakerOpen):
        b.guard()
