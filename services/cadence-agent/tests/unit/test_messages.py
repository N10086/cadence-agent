"""L2-4 错误转译：NOT_IMPLEMENTED(501) / UPSTREAM_UNAVAILABLE(503) 必须给人话 + suggestions。

判定标准只有两条，且都必须可程序化检查：

1. 用户看到的文本里**不出现协议码/原始 message**（501、NOT_IMPLEMENTED、Core 的英文/内部文案）；
2. 一定带 `suggestions`（结构化的下一步），而不只是一句"出错了"。

码本身保持原样 —— 评测（L4）按码统计，用户文案不参与判定。
"""

from __future__ import annotations

from cadence.runtime.errors import ErrorCode, ToolError
from cadence.runtime.messages import (
    humanize,
    humanize_event,
    redact_protocol_text,
)

#: 501 的原始 message 长什么样不重要，重要的是它**不许**出现在用户文本里
RAW_501 = "取消当前版本不支持（NOT_IMPLEMENTED 501）"


def test_not_implemented_is_translated_and_never_leaks_raw_message() -> None:
    err = ToolError(ErrorCode.NOT_IMPLEMENTED, RAW_501)
    text, suggestions = humanize(err)

    assert "NOT_IMPLEMENTED" not in text
    assert "501" not in text
    assert RAW_501 not in text
    assert "取消当前版本不支持" not in text
    assert "做不到" in text or "暂不支持" in text
    assert suggestions, "必须给出下一步建议"
    assert all("hint" in s for s in suggestions)


def test_upstream_unavailable_is_translated_with_suggestions() -> None:
    err = ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "Core 服务异常（503）")
    text, suggestions = humanize(err)
    assert "503" not in text
    assert "上游" in text
    assert suggestions


def test_core_down_uses_a_different_message_than_provider_down() -> None:
    core = ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "Core 连接失败", core_down=True)
    provider = ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "provider 503")

    core_text, _ = humanize(core)
    provider_text, _ = humanize(provider)

    assert core_text != provider_text, "Core 挂了和模型挂了是两件事，不能同一句话"
    assert "任务服务" in core_text
    assert any("Core" in s["hint"] for s in humanize(core)[1])


def test_redact_protocol_text_strips_codes_from_model_paraphrase() -> None:
    """模型会原样复述工具错误；clarify/done 文本必须洗干净。"""
    dirty = "这个操作返回 NOT_IMPLEMENTED(501)，所以我没法取消。"
    clean = redact_protocol_text(dirty)
    assert "NOT_IMPLEMENTED" not in clean
    assert "501" not in clean
    assert "取消" in clean


def test_humanize_event_keeps_code_but_swaps_message() -> None:
    err = ToolError(ErrorCode.NOT_IMPLEMENTED, RAW_501)
    event = humanize_event(err)
    assert event["code"] == "NOT_IMPLEMENTED", "码必须保留，评测要按码统计"
    assert "NOT_IMPLEMENTED" not in event["message"]
    assert "501" not in event["message"]
    assert event["suggestions"]


def test_other_error_messages_are_not_overwritten() -> None:
    """业务错误（PLAN_STALE / 冲突）本身就是人话，不该被套模板。"""
    err = ToolError(ErrorCode.PLAN_STALE, "任务 t_001 已被修改，请重新预览后再确认")
    text, _ = humanize(err)
    assert text == "任务 t_001 已被修改，请重新预览后再确认"
