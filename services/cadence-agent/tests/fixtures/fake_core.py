"""内存版 Core，形状与 CoreClient.request 一致。

为什么不在集成测试里 mock HTTP：Agent 的失败矩阵大多是**业务语义**的
（409 版本冲突、422 依赖未完成、404 已被删），用 HTTP mock 要造一堆响应体，
而用内存实现可以直接复现"两个人同时改一条任务"这种真实竞态。

core_client 自己的 HTTP 行为（状态码翻译、重试退避）由 tests/unit/test_core_client.py
用 httpx.MockTransport 单独测 —— 两层各自测，互不掩盖。
"""

from __future__ import annotations

import copy
from typing import Any

from cadence.runtime.errors import ErrorCode, ToolError


def _task(tid: str, title: str, **kw: Any) -> dict[str, Any]:
    base = {
        "id": tid,
        "title": title,
        "description": None,
        "status": "TODO",
        "priority": "NORMAL",
        "startAt": None,
        "dueAt": None,
        "estimateMin": None,
        "parentId": None,
        "pinned": False,
        "rrule": None,
        "tags": [],
        "version": 1,
        "createdAt": "2026-11-08T09:00:00+08:00",
        "updatedAt": "2026-11-08T09:00:00+08:00",
    }
    base.update(kw)
    return base


class FakeCore:
    def __init__(self, tasks: list[dict[str, Any]] | None = None) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        for t in tasks or []:
            self.tasks[t["id"]] = copy.deepcopy(t)
        self.calls: list[dict[str, Any]] = []
        self.dependencies: dict[str, list[str]] = {}
        self.idempotency: dict[str, dict[str, Any]] = {}
        #: 下一次请求直接抛这个错（用于 F4 工具 5xx）
        self.next_error: ToolError | None = None
        self.raise_times = 0
        #: 每次请求前调用，用于模拟"另一个人在这期间改了/删了数据"
        self.on_request: Any = None

    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        trace_id: str | None = None,
        actor: str = "agent",
        confirm_token: str | None = None,
        idempotency_key: str | None = None,
        tool_name: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "body": body,
                "trace_id": trace_id,
                "actor": actor,
                "idempotency_key": idempotency_key,
                "confirm_token": confirm_token,
                "tool_name": tool_name,
            }
        )

        if self.next_error is not None and self.raise_times > 0:
            self.raise_times -= 1
            raise self.next_error

        if self.on_request is not None:
            self.on_request(method, path)

        if method == "GET" and path == "/api/v1/tasks":
            return self._list(params or {})
        if method == "GET" and path == "/api/v1/calendar/busy":
            return {"windows": []}
        if method == "GET" and path.startswith("/api/v1/tasks/"):
            tid = path.rsplit("/", 1)[-1]
            task = self._get(tid)
            return {
                "task": copy.deepcopy(task),
                "dependsOn": self.dependencies.get(tid, []),
                "blocks": [],
                "conflicts": [],
            }
        if method == "POST" and path == "/api/v1/tasks":
            return self._create(body or {}, idempotency_key)
        if method == "POST" and path.endswith(":complete"):
            tid = path[len("/api/v1/tasks/") : -len(":complete")]
            return self._transition(tid, (body or {}).get("version"), "DONE")
        if path.endswith("/dependencies") and method == "POST":
            tid = path[len("/api/v1/tasks/") : -len("/dependencies")]
            dep = (body or {}).get("dependsOnId")
            self.dependencies.setdefault(tid, []).append(dep)
            return [dep]
        if method == "PATCH" and path.startswith("/api/v1/tasks/"):
            tid = path.rsplit("/", 1)[-1]
            return self._patch(tid, body or {})
        if method == "DELETE" and path.startswith("/api/v1/tasks/"):
            tid = path.rsplit("/", 1)[-1]
            return self._delete(tid, confirm_token)
        raise ToolError(ErrorCode.UPSTREAM_UNAVAILABLE, f"FakeCore 没实现 {method} {path}")

    # ------------------------------------------------------------------

    def _get(self, tid: str) -> dict[str, Any]:
        task = self.tasks.get(tid)
        if task is None:
            raise ToolError(ErrorCode.TASK_ID_NOT_FOUND, f"任务 {tid} 不存在")
        return task

    def _list(self, params: dict[str, Any]) -> dict[str, Any]:
        rows = list(self.tasks.values())
        if params.get("id"):
            rows = [t for t in rows if t["id"] == params["id"]]
        if params.get("keyword"):
            kw = str(params["keyword"]).lower()
            rows = [t for t in rows if kw in t["title"].lower()]
        if params.get("status"):
            rows = [t for t in rows if t["status"] == params["status"]]
        if params.get("priority"):
            rows = [t for t in rows if t["priority"] == params["priority"]]
        if params.get("tag"):
            rows = [t for t in rows if params["tag"] in (t.get("tags") or [])]
        limit = int(params.get("limit") or 20)
        total = len(rows)
        return {"items": copy.deepcopy(rows[:limit]), "hasMore": total > limit, "total": total}

    def _create(self, body: dict[str, Any], idempotency_key: str | None) -> dict[str, Any]:
        if idempotency_key and idempotency_key in self.idempotency:
            return copy.deepcopy(self.idempotency[idempotency_key])
        tid = f"t_{len(self.tasks) + 1:03d}"
        task = _task(
            tid,
            body.get("title", ""),
            status="TODO",
            priority=body.get("priority", "NORMAL"),
            startAt=body.get("startAt"),
            dueAt=body.get("dueAt"),
            estimateMin=body.get("estimateMin"),
            parentId=body.get("parentId"),
            pinned=body.get("pinned", False),
            rrule=body.get("rrule"),
            tags=body.get("tags") or [],
            description=body.get("description"),
            version=0,
        )
        self.tasks[tid] = task
        if idempotency_key:
            self.idempotency[idempotency_key] = copy.deepcopy(task)
        return copy.deepcopy(task)

    def _patch(self, tid: str, body: dict[str, Any]) -> dict[str, Any]:
        task = self._get(tid)
        version = body.get("version")
        if version is not None and int(version) != task["version"]:
            raise ToolError(ErrorCode.CONFLICT, "版本冲突：任务在操作期间被修改")
        for key in ("title", "description", "priority", "startAt", "dueAt", "estimateMin", "pinned", "rrule", "tags"):
            if key in body:
                task[key] = body[key]
        task["version"] += 1
        return {"task": copy.deepcopy(task), "warnings": []}

    def _transition(self, tid: str, version: Any, target: str) -> dict[str, Any]:
        task = self._get(tid)
        if task["status"] == target:
            return copy.deepcopy(task)  # 幂等
        if version is not None and int(version) != task["version"]:
            raise ToolError(ErrorCode.CONFLICT, "版本冲突：任务在操作期间被修改")
        if task["status"] == "CANCELLED":
            raise ToolError(ErrorCode.ILLEGAL_TRANSITION, "已取消的任务不能标记完成")
        task["status"] = target
        task["version"] += 1
        return copy.deepcopy(task)

    def _delete(self, tid: str, confirm_token: str | None) -> None:
        self._get(tid)
        if not confirm_token:
            raise ToolError(
                ErrorCode.CONFIRM_REQUIRED,
                "删除需要确认令牌",
                details={"confirmToken": "ct_fake", "snapshot": copy.deepcopy(self.tasks[tid])},
            )
        if confirm_token != "ct_fake":
            raise ToolError(ErrorCode.CONFIRM_REQUIRED, "令牌无效")
        self.tasks.pop(tid, None)
        return None

    # ------------------------------------------------------------------

    def mutate_externally(self, tid: str, **changes: Any) -> None:
        """模拟"另一个人在你操作期间改了这条任务" —— 测试 409 用。"""
        task = self.tasks[tid]
        task.update(changes)
        task["version"] += 1

    def remove_externally(self, tid: str) -> None:
        """模拟并发删除 —— 这是 TASK_ID_NOT_FOUND 唯一能真实发生的场景。"""
        self.tasks.pop(tid, None)
