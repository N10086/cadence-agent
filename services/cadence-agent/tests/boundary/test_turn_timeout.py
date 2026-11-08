"""L2-3 的单轮总超时：60s 到了必须收尾，而不是让页面一直转圈。

这里用 0.2s 的假超时把真实的 60s 压缩到测试可接受的范围 —— 测的是**机制**
（asyncio.timeout 包住 run_turn + 收尾事件），不是那个数字本身（数字由 config 测试钉住）。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from cadence.deps import set_runtime
from cadence.main import create_app


class SlowClient:
    """永远在生成中的流：模拟"规划+工具"整体拖过单轮预算。"""

    name = "slow"

    async def chat(self, *a, **k):  # pragma: no cover
        raise AssertionError("不该调用")

    async def stream(self, *a, **k):
        for _ in range(1000):
            await asyncio.sleep(0.05)
            from cadence.llm.base import LLMEvent

            yield LLMEvent(kind="token", text="…")


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


def test_turn_timeout_closes_the_stream_with_visible_error(client, runtime) -> None:
    runtime.settings.turn_timeout_s = 0.2
    runtime.llm = SlowClient()
    runtime.loop.llm = SlowClient()
    runtime.loop.fast_llm = runtime.loop.llm

    r = client.post("/chat", json={"sessionId": "s_turn_timeout", "message": "看看任务"})
    assert r.status_code == 202
    events = read_events(client, r.json()["data"]["streamId"])

    kinds = [e["event"] for e in events]
    assert kinds[-1] == "done", "超时也必须以 done 收尾，否则前端永远等"

    done = events[-1]["data"]
    assert done["endedBy"] == "timeout"
    assert done["errorCode"] == "TURN_TIMEOUT"
    assert "60 秒" in done["text"] or "超过" in done["text"]
    assert done["suggestions"], "超时要给下一步建议"
    assert any(e["event"] == "error" and e["data"]["code"] == "TURN_TIMEOUT" for e in events)
