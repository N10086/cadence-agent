"""SSE 事件名。

放在 runtime 层而不是 api 层：循环（runtime）要发事件，而 api 只是把事件编码成帧。
如果常量住在 api，runtime 就得反过来依赖 api —— 那是分层倒置。
"""

from __future__ import annotations

EVENT_TOKEN = "token"
EVENT_TOOL_CALL = "tool_call"
EVENT_TOOL_RESULT = "tool_result"
EVENT_CLARIFY = "clarify"
EVENT_CONFIRM = "confirm"
EVENT_DONE = "done"
EVENT_ERROR = "error"

ALL_EVENTS = (
    EVENT_TOKEN,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    EVENT_CLARIFY,
    EVENT_CONFIRM,
    EVENT_DONE,
    EVENT_ERROR,
)
