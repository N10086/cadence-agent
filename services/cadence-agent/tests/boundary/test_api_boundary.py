"""HTTP / SSE 层的边界测试。

对齐 docs/接口文档.md 第七节：错误外壳、202、SSE 续传、/confirm 的
approve 语义、trace 路径安全、health 的裸对象。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from cadence.deps import set_runtime
from cadence.llm.fake import call, say
from cadence.main import create_app
from cadence.runtime.errors import ErrorCode, ToolError


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


# =====================================================================
# /chat 请求校验
# =====================================================================


def test_chat_missing_fields_is_400(client) -> None:
    r = client.post("/chat", json={})
    assert r.status_code == 400
    assert r.json()["code"] == 400
    assert "message" in r.json()


def test_chat_message_too_long_is_400(client) -> None:
    r = client.post("/chat", json={"sessionId": "s", "message": "x" * 2001})
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_chat_session_id_too_long_is_400(client) -> None:
    r = client.post("/chat", json={"sessionId": "s" * 101, "message": "hi"})
    assert r.status_code == 400


def test_chat_whitespace_only_message_currently_accepted(client, runtime) -> None:
    """BUG-SP2-20：min_length=1 允许全空格；Agent 会进 clarify 而不是 400。"""
    runtime.llm.load([say("x")])
    r = client.post("/chat", json={"sessionId": "s_ws", "message": "   "})
    assert r.status_code == 202
    events = read_events(client, r.json()["data"]["streamId"])
    assert any(e["event"] == "clarify" for e in events)


@pytest.mark.xfail(strict=True, reason="BUG-SP2-20：只有空白的 message 应作为参数错误拒绝")
def test_bug_chat_whitespace_message_rejected(client, runtime) -> None:
    runtime.llm.load([say("x")])
    r = client.post("/chat", json={"sessionId": "s_ws2", "message": "   "})
    assert r.status_code == 400


def test_chat_returns_202_and_stream_ids(client, runtime) -> None:
    runtime.llm.load([say("你好")])
    r = client.post("/chat", json={"sessionId": "s_202", "message": "你好"})
    assert r.status_code == 202
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["streamId"].startswith("st_")
    assert body["data"]["traceId"].startswith("tr_")
    assert body["data"]["degraded"] is False


# =====================================================================
# SSE
# =====================================================================


def test_stream_unknown_id_is_404_envelope(client) -> None:
    r = client.get("/stream/st_nope")
    assert r.status_code == 404
    assert r.json()["code"] == 404


def test_stream_done_event_contains_ended_by_and_actions(client, runtime) -> None:
    runtime.llm.load([call("task.search", limit=10), say("查到了")])
    r = client.post("/chat", json={"sessionId": "s_sse", "message": "看看任务"})
    events = read_events(client, r.json()["data"]["streamId"])
    done = events[-1]
    assert done["event"] == "done"
    for key in ("traceId", "costYuan", "degraded", "degradeReason", "steps", "endedBy", "actionsTaken"):
        assert key in done["data"], key


def test_stream_resume_from_last_event_id(client, runtime) -> None:
    runtime.llm.load([call("task.search", limit=10), say("查到了")])
    r = client.post("/chat", json={"sessionId": "s_resume", "message": "看看任务"})
    stream_id = r.json()["data"]["streamId"]
    first = read_events(client, stream_id)
    assert len(first) >= 3
    after = first[1]["id"]

    resumed = []
    with client.stream("GET", f"/stream/{stream_id}", headers={"Last-Event-ID": str(after)}) as resp:
        current = {}
        for line in resp.iter_lines():
            if line.startswith("event:"):
                current["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                current["data"] = json.loads(line.split(":", 1)[1].strip())
            elif line.startswith("id:"):
                current["id"] = int(line.split(":", 1)[1].strip())
            elif line == "" and current:
                resumed.append(current)
                if current["event"] == "done":
                    break
                current = {}
    assert resumed
    assert all(e["id"] > after for e in resumed)


# =====================================================================
# /confirm
# =====================================================================


def test_confirm_requires_token_field(client) -> None:
    r = client.post("/confirm", json={})
    assert r.status_code == 400


def test_confirm_unknown_token_is_409(client) -> None:
    r = client.post("/confirm", json={"confirmToken": "cf_nope", "approve": True})
    assert r.status_code == 409
    assert r.json()["code"] == 409


def test_confirm_decline_is_noop(client, runtime, core) -> None:
    token = runtime.confirms.issue(tool="task.delete", args={"task_id": "t_001"}).token
    r = client.post("/confirm", json={"confirmToken": token, "approve": False})
    assert r.status_code == 200
    assert r.json()["data"]["applied"] == 0
    assert "t_001" in core.tasks


def test_confirm_requires_explicit_approve(client, runtime, core) -> None:
    token = runtime.confirms.issue(tool="task.delete", args={"task_id": "t_001"}).token
    r = client.post("/confirm", json={"confirmToken": token})
    assert r.status_code in (400, 422)
    assert "t_001" in core.tasks


def test_confirm_token_single_use_is_409(client, runtime, core) -> None:
    token = runtime.confirms.issue(tool="task.delete", args={"task_id": "t_001"}).token
    assert client.post("/confirm", json={"confirmToken": token, "approve": True}).status_code == 200
    r = client.post("/confirm", json={"confirmToken": token, "approve": True})
    assert r.status_code == 409


def test_confirm_split_creates_children(client, runtime, core) -> None:
    runtime.llm.load([call(
        "task.split",
        children=[{"title": f"c{i}"} for i in range(4)],
        parent_title="准备面试",
    )])
    r = client.post("/chat", json={"sessionId": "s_split2", "message": "帮我拆成 4 步"})
    events = read_events(client, r.json()["data"]["streamId"])
    token = [e for e in events if e["event"] == "confirm"][0]["data"]["confirmToken"]
    before = set(core.tasks)
    done = client.post("/confirm", json={"confirmToken": token, "approve": True})
    assert done.status_code == 200
    assert done.json()["data"]["applied"] == 5  # 父 + 4 子，锁住 L0-3 的语义
    assert done.json()["data"]["parentId"]
    assert len(set(core.tasks) - before) == 5  # 父 + 4 子


# =====================================================================
# /trace
# =====================================================================


def test_trace_unknown_is_404(client) -> None:
    r = client.get("/trace/tr_nope")
    assert r.status_code == 404
    assert r.json()["code"] == 404


def test_trace_rejects_path_traversal(client) -> None:
    r = client.get("/trace/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 404


def test_trace_uses_camel_case_and_has_summary(client, runtime) -> None:
    runtime.llm.load([call("task.search", limit=10), say("查到了")])
    r = client.post("/chat", json={"sessionId": "s_trace", "message": "看看任务"})
    trace_id = r.json()["data"]["traceId"]
    read_events(client, r.json()["data"]["streamId"])

    t = client.get(f"/trace/{trace_id}").json()["data"]
    assert t["traceId"] == trace_id
    assert t["totalMs"] >= 0
    assert "costYuan" in t and "degraded" in t and "promptVersion" in t
    assert t["steps"]
    for s in t["steps"]:
        assert "durationMs" in s and "tokensIn" in s and "tokensOut" in s


# =====================================================================
# /health
# =====================================================================


def test_health_is_bare_object(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "code" not in body
    assert body["status"] in ("UP", "DOWN")
    assert "core" in body


def test_health_core_down(client, runtime, core) -> None:
    core.next_error = ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, "core down")
    core.raise_times = 10
    body = client.get("/health").json()
    assert body["status"] == "DOWN"
    assert "DOWN" in body["core"]


# =====================================================================
# 会话：澄清问题是否进入历史
# =====================================================================


def test_clarify_question_is_not_saved_in_history_current_behavior(client, runtime) -> None:
    """BUG-SP2-07：clarify 轮次不写 assistant 消息，下一轮看不到上一句问了什么。"""
    r = client.post("/chat", json={"sessionId": "s_clar", "message": "把那个搞一下"})
    read_events(client, r.json()["data"]["streamId"])

    path = runtime.settings.resolved_session_dir / "s_clar.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    roles = [t["role"] for t in data["recent_turns"]]
    assert roles == ["user"]
    assert data["summary"] == ""


@pytest.mark.xfail(strict=True, reason="BUG-SP2-07：clarify 的问题必须进入会话历史，"
                                       "否则用户回答'周五'时上下文里没有'周五是什么问题'")
def test_bug_clarify_question_is_saved(client, runtime) -> None:
    r = client.post("/chat", json={"sessionId": "s_clar2", "message": "把那个搞一下"})
    read_events(client, r.json()["data"]["streamId"])
    path = runtime.settings.resolved_session_dir / "s_clar2.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    roles = [t["role"] for t in data["recent_turns"]]
    assert "assistant" in roles
