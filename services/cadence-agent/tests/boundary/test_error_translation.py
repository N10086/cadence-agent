"""L2-4 的 HTTP 层验收：用户看到的 SSE 里不许有协议码，且 done 带 degraded/suggestions/promptVersion。

这一层单独测的理由：转译函数对不代表链路对。真正要钉住的是**用户实际收到的那几帧**：
`error` 事件的 message、`done` 的 text/suggestions/degradeReason。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cadence.deps import set_runtime
from cadence.llm.fake import call, fail, say
from cadence.main import create_app
from cadence.tools.base import Tool
from cadence.guard.permissions import Permission
from cadence.runtime.errors import ErrorCode, ToolError

from pydantic import BaseModel


class _Args(BaseModel):
    task_id: str


@pytest.fixture
def client(runtime):
    set_runtime(runtime)
    app = create_app()
    with TestClient(app) as c:
        yield c
    set_runtime(None)


def read_events(client: TestClient, stream_id: str, *, limit: int = 200) -> list[dict]:
    events: list[dict] = []
    current: dict = {}
    with client.stream("GET", f"/stream/{stream_id}") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("event:"):
                current["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                current["data"] = json.loads(line.split(":", 1)[1].strip())
            elif line.startswith("id:"):
                current["id"] = int(line.split(":", 1)[1].strip())
            elif line == "" and current:
                events.append(current)
                if current["event"] == "done":
                    break
                current = {}
            if len(events) > limit:
                break
    return events


async def _raise_not_implemented(_args, _ctx):
    raise ToolError(ErrorCode.NOT_IMPLEMENTED, "取消当前版本不支持（NOT_IMPLEMENTED 501）")


def test_not_implemented_never_reaches_user_as_raw_message(client, runtime, monkeypatch) -> None:
    # 工具名不在权限矩阵里时默认按 DESTRUCTIVE 处理（会先要确认）。
    # 这里只想看"能力未实现"的转译，所以显式标成 READ。
    from cadence.guard import permissions as perms

    monkeypatch.setitem(perms.TOOL_PERMISSIONS, "task.cancel", perms.Permission.READ)
    runtime.registry.register(
        Tool(
            name="task.cancel",
            description="取消任务（本版未实现）",
            handler=_raise_not_implemented,
            args_model=_Args,
            permission=Permission.READ,
        )
    )
    runtime.llm.load(
        [
            call("task.search", limit=5),
            call("task.cancel", task_id="t_001"),
            say("这个操作我目前还做不到，可以先帮你标记完成吗？"),
        ]
    )

    r = client.post("/chat", json={"sessionId": "s_501", "message": "把买牛奶取消掉"})
    assert r.status_code == 202
    events = read_events(client, r.json()["data"]["streamId"])

    # 用户可见的文案（error.message / done.text / clarify.question）里不许有协议码。
    # `code` 字段本身保留 —— 那是给评测统计用的，不面向用户。
    for e in events:
        if e["event"] == "error":
            msg = e["data"].get("message", "")
            assert "NOT_IMPLEMENTED" not in msg
            assert "501" not in msg
    done = events[-1]
    assert done["event"] == "done"
    assert "NOT_IMPLEMENTED" not in done["data"]["text"]
    assert "501" not in done["data"]["text"]
    assert "做不到" in done["data"]["text"]
    assert done["data"]["suggestions"], "done 必须带下一步建议"
    assert done["data"]["degraded"] is False
    assert done["data"]["promptVersion"] == "4", "L4 报告头需要 prompt_version"
    # 码是给机器看的：即使模型把 501 消化成了一句正常回答，done 里也要留下这个码
    assert done["data"]["errorCode"] == "NOT_IMPLEMENTED"


def test_done_carries_degraded_flag_and_reason(client, runtime) -> None:
    runtime.settings.llm_retry_times = 0
    runtime.llm.load([fail("timeout"), fail("timeout"), say("单步回答")])

    r = client.post("/chat", json={"sessionId": "s_deg", "message": "帮我准备面试"})
    events = read_events(client, r.json()["data"]["streamId"])
    done = events[-1]["data"]

    assert done["degraded"] is True
    assert done["degradeReason"].startswith("single_step_fallback")
    assert done["promptVersion"] == "4"
    # 降级必须在流里留下痕迹，而不是只体现在 done 的一个布尔值
    degrade_frames = [e for e in events if e["event"] == "error" and e["data"].get("degraded")]
    assert degrade_frames, "降级必须有 error 事件（DEGRADED）"


def test_upstream_error_done_text_is_humanized(client, runtime) -> None:
    """上游全挂：done.text 是人话 + 有建议，且 degraded 为 True（降级过）。"""
    runtime.settings.llm_retry_times = 0

    class Down:
        name = "down"

        async def chat(self, *a, **k):  # pragma: no cover
            raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "boom")

        async def stream(self, *a, **k):
            from cadence.llm.base import LLMUnavailable

            raise LLMUnavailable("provider down 503")
            yield  # pragma: no cover

    runtime.llm = Down()
    runtime.loop.llm = Down()
    runtime.loop.fast_llm = Down()

    r = client.post("/chat", json={"sessionId": "s_503", "message": "看看任务"})
    events = read_events(client, r.json()["data"]["streamId"])
    done = events[-1]["data"]

    for e in events:
        if e["event"] == "error":
            assert "503" not in e["data"].get("message", "")
    assert "503" not in done["text"]
    assert "上游" in done["text"]
    assert done["suggestions"]
    assert done["errorCode"] == "UPSTREAM_UNAVAILABLE"
