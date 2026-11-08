"""错误转译：把内部错误码 / 上游协议码翻成用户能读懂的话 + 下一步建议。

为什么单独一层，而不是在 `api/chat.py` 里写 if：

1. **Core 的 501/503 是协议细节，不是用户语言。** 「NOT_IMPLEMENTED(501)」、
   「Core 服务异常（503）」直接透给用户，等于把我们的实现细节甩给他。
   L2 要求这两类必须包成人话 + suggested next step，且**不许原样透传 message**。
2. **同一句话要给三个消费者看**：SSE `error` 事件、SSE `done.text`（用户最终看到的
   那句话）、以及测试断言。放在一个纯函数里，三方看到的一定是同一句。
3. **翻译不改变错误码**。码是给评测和 trace 用的（L4 要按码统计），文案只面向人。
   两者分开，才能做到「机器可判定、人可理解」。

这里只做"人话 + 建议"，不吞错：调用方仍然拿到 `ToolError.code` / `recoverable`。
"""

from __future__ import annotations

import re
from typing import Any

from .errors import ErrorCode, ToolError


def humanize(error: ToolError) -> tuple[str, list[dict[str, Any]]]:
    """返回 `(给用户的一句话, 建议列表)`。

    建议是结构化的（`{"hint": ..., "reason": ...}`），前端/客户端可以渲染成按钮；
    同时也会拼进文案，保证纯文本客户端也不丢信息。
    """
    if error.core_down:
        text = "任务服务（Core）现在连不上，这一轮没有改成任何数据。等它恢复后我再继续。"
        suggestions = [
            {"hint": "稍等几十秒后让我重试", "reason": "Core 超时或返回 5xx，通常是短暂抖动"},
            {"hint": "如果一直这样，检查 Core 是否在运行（/health 会显示 DOWN）", "reason": "依赖不可用"},
        ]
        return _with_tail(text, suggestions), suggestions

    code = error.code

    if code is ErrorCode.NOT_IMPLEMENTED:
        # ⚠️ 不贴 error.message：那是协议码/内部能力描述。
        text = "这个操作我目前还做不到，已经停在这里，没有动你的数据。"
        suggestions = [
            {"hint": "换个我能做的操作：搜索 / 新建 / 改标题或时间 / 标记完成 / 删除", "reason": "当前能力边界"},
            {"hint": "要取消任务的话，我可以先帮你标记完成或删除", "reason": "取消能力未实现"},
        ]
        return _with_tail(text, suggestions), suggestions

    if code is ErrorCode.UPSTREAM_UNAVAILABLE:
        text = "上游服务暂时不可用，这一轮没有完成。已经完成的部分我会如实保留。"
        suggestions = [
            {"hint": "稍后再让我试一次", "reason": "上游不可用通常是暂时的"},
            {"hint": "如果反复出现，用 /health 看是 Core 还是模型侧的问题", "reason": "定位依赖"},
        ]
        return _with_tail(text, suggestions), suggestions

    if code is ErrorCode.RATE_LIMITED:
        text = "请求太频繁了，被上游限流了。我停一下，没有继续重试烧钱。"
        suggestions = [
            {"hint": "等几秒再发一次", "reason": "429 通常带 Retry-After"},
            {"hint": "把一次要做的事拆小一点", "reason": "减少单轮调用次数"},
        ]
        return _with_tail(text, suggestions), suggestions

    if code is ErrorCode.BUDGET_EXCEEDED:
        text = "这一轮做不完了（步数或 token 预算用尽），我先汇报已经完成的部分。"
        suggestions = [
            {"hint": "说「继续」，我接着做剩下的", "reason": "预算按轮计算"},
            {"hint": "一次只让我做一件事，成功率更高", "reason": "减少规划轮次"},
        ]
        return _with_tail(text, suggestions), suggestions

    if code is ErrorCode.TURN_TIMEOUT:
        text = "这一轮超过 60 秒还没做完，我已经把它停下来了，不会让你一直等。"
        suggestions = [
            {"hint": "把这件事拆成两三次说", "reason": "单轮有 60s 上限"},
            {"hint": "先说「继续」，我从已完成的进度往下做", "reason": "动作不会回滚"},
        ]
        return _with_tail(text, suggestions), suggestions

    if code is ErrorCode.OVERLOADED:
        text = "现在同时在处理的请求太多了，我这边先把这次挡下来，没有让它排队干等。"
        suggestions = [
            {"hint": "几秒后重试", "reason": "入口有界队列，满即 503 而不是无限排队"},
            {"hint": "少开几个并发的会话", "reason": "队列容量是 64"},
        ]
        return _with_tail(text, suggestions), suggestions

    if code is ErrorCode.SESSION_BUSY:
        text = "这个会话的上一条消息还在处理，我等了 20 秒也没轮到，所以先放弃这次。"
        suggestions = [
            {"hint": "等上一条处理完再发", "reason": "同一会话串行，避免上下文互相覆盖"},
            {"hint": "换一个新会话发这条", "reason": "不同会话互不影响"},
        ]
        return _with_tail(text, suggestions), suggestions

    # 其余错误码：message 本身是业务语言（「任务已被修改」「依赖未完成」），可以直出。
    return error.message, list(error.suggestions)


def _with_tail(text: str, suggestions: list[dict[str, Any]]) -> str:
    hints = [s.get("hint", "") for s in suggestions if s.get("hint")]
    if not hints:
        return text
    return text + " 你可以：" + "；".join(hints) + "。"


def humanize_event(error: ToolError) -> dict[str, Any]:
    """SSE `error` 事件的数据体。码保持原样，message 换成用户语言。"""
    text, suggestions = humanize(error)
    return {
        "code": error.code.value,
        "message": text,
        "recoverable": bool(error.recoverable),
        "suggestions": suggestions,
    }


#: 用户可见文本里不该出现的协议痕迹：只抹**协议上下文**，不抹裸数字。
#:
#: 旧实现是 `str.replace("503", "")`，它会把用户的正常内容一起吃掉：
#: 「建个任务：排查 503 报错」→「建个任务：排查  报错」。
#: 数字本身可能是用户业务数据（端口、错误码、体积），只有跟着协议说法的才该抹。
_PROTOCOL_NAME = re.compile(r"\bNOT_IMPLEMENTED\b")
_UPSTREAM_NAME = re.compile(r"\bUPSTREAM_UNAVAILABLE\b")
_HTTP_CODE_WITH_PREFIX = re.compile(
    r"(?:HTTP\s*|状态码\s*|错误码\s*|code\s*[=:]\s*)[（(]?\s*(501|503)\s*[)）]?"
)
_BARE_PAREN_CODE = re.compile(r"[（(]\s*(501|503)\s*[)）]")


def redact_protocol_text(text: str) -> str:
    """把"协议码"从**用户可见**文本里抹掉，但保留业务语义里的裸数字。

    模型在 clarify 里经常原样复述工具错误（"NOT_IMPLEMENTED (501)：取消当前版本不支持"）。
    码本身必须留在 trace/评测里，但用户那句话不能长这样。

    只处理四类协议形态（与 L2-4 的验收口径一致）：
      - 协议名：`NOT_IMPLEMENTED` / `UPSTREAM_UNAVAILABLE` → 人话
      - 带前缀的状态码：`HTTP 503` / `状态码 503` / `错误码 501` / `code=503`
      - 括号里的状态码：`(501)` / `（503）`
      - **不处理**裸 `503`：「排查 503 报错」里的 503 是用户的业务内容。
    """
    out = text or ""
    out = _PROTOCOL_NAME.sub("暂不支持", out)
    out = _UPSTREAM_NAME.sub("上游不可用", out)
    out = _HTTP_CODE_WITH_PREFIX.sub("", out)
    out = _BARE_PAREN_CODE.sub("", out)
    return out.strip()
