"""HTTP 契约：5 个端点 + SSE 事件。

对齐 docs/接口文档.md §7。前端和评测脚本只依赖这里的行为。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from cadence.deps import set_runtime
from cadence.llm.fake import call, say
from cadence.main import create_app


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


# ---------------------------------------------------------------------------


def test_health_is_bare_object(client, runtime) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("UP", "DOWN")
    assert "core" in body
    assert "code" not in body, "健康检查不包外壳，给运维探测用"


def test_chat_returns_202_and_stream_id(client, runtime) -> None:
    runtime.llm.load([say("你好。")])
    resp = client.post("/chat", json={"sessionId": "s_api1", "message": "你好"})

    assert resp.status_code == 202
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["streamId"].startswith("st_")
    assert body["data"]["traceId"].startswith("tr_")
    assert body["data"]["degraded"] is False


def test_stream_pushes_events_and_done(client, runtime) -> None:
    runtime.llm.load([call("task.search", limit=5), say("你有两个任务。")])
    resp = client.post("/chat", json={"sessionId": "s_api2", "message": "看看我的任务"})
    stream_id = resp.json()["data"]["streamId"]

    events = read_events(client, stream_id)
    kinds = [e["event"] for e in events]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "token" in kinds
    assert kinds[-1] == "done"

    done = [e for e in events if e["event"] == "done"][0]["data"]
    assert done["endedBy"] == "finish"
    assert done["steps"] >= 2
    assert "traceId" in done


def test_trace_endpoint_returns_steps(client, runtime) -> None:
    runtime.llm.load([call("task.search", limit=5), say("好了。")])
    resp = client.post("/chat", json={"sessionId": "s_api3", "message": "看看任务"})
    trace_id = resp.json()["data"]["traceId"]
    read_events(client, resp.json()["data"]["streamId"])

    trace = client.get(f"/trace/{trace_id}")
    assert trace.status_code == 200
    data = trace.json()["data"]
    assert data["traceId"] == trace_id
    assert data["steps"], "trace 不能是空的"
    kinds = {s["kind"] for s in data["steps"]}
    assert "LLM" in kinds and "TOOL" in kinds


def test_unknown_stream_returns_404_envelope(client) -> None:
    resp = client.get("/stream/st_nope")
    assert resp.status_code == 404
    assert resp.json()["code"] == 404


def test_unknown_trace_returns_404(client) -> None:
    resp = client.get("/trace/tr_nope")
    assert resp.status_code == 404
    assert resp.json()["code"] == 404


def test_confirm_with_bad_token_returns_409(client) -> None:
    resp = client.post("/confirm", json={"confirmToken": "cf_nope", "approve": True})
    assert resp.status_code == 409
    assert resp.json()["code"] == 409


def test_confirm_decline_is_noop(client, runtime) -> None:
    token = runtime.confirms.issue(tool="task.delete", args={"task_id": "t_001"}).token
    resp = client.post("/confirm", json={"confirmToken": token, "approve": False})
    assert resp.status_code == 200
    assert resp.json()["data"]["applied"] == 0


def test_confirm_executes_delete(client, runtime, core) -> None:
    token = runtime.confirms.issue(tool="task.delete", args={"task_id": "t_001"}).token
    resp = client.post("/confirm", json={"confirmToken": token, "approve": True})
    assert resp.status_code == 200
    assert resp.json()["data"]["applied"] == 1
    assert "t_001" not in core.tasks


def test_invalid_body_keeps_envelope(client) -> None:
    resp = client.post("/chat", json={"sessionId": "s1"})  # 缺 message
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 400
    assert "message" in body


def test_delete_confirm_flow_over_http(client, runtime, core) -> None:
    """两阶段删除的完整 HTTP 路径：/chat → confirm 事件 → /confirm → 数据没了。"""
    runtime.llm.load([call("task.search", limit=5), call("task.delete", task_id="t_001")])
    resp = client.post("/chat", json={"sessionId": "s_api4", "message": "把买牛奶删掉"})

    events = read_events(client, resp.json()["data"]["streamId"])
    confirm_events = [e for e in events if e["event"] == "confirm"]
    assert confirm_events, "必须推 confirm 事件"
    token = confirm_events[0]["data"]["confirmToken"]
    assert "t_001" in core.tasks

    done = client.post("/confirm", json={"confirmToken": token, "approve": True})
    assert done.status_code == 200
    assert "t_001" not in core.tasks
