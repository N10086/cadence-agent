"""每条用例的隔离沙箱（L4-2）。

流水线：`reset → seed(走 API) → run turn → snapshot`。

- `--core fake`：复用 `tests/fixtures/fake_core.FakeCore`（不新写第二套假 Core）；
- `--core live`：同一个 `CoreClient`，走真实 HTTP；reset 用两阶段 DELETE。

**reset 不用 SQL**：Agent 的隔离红线是"只通过 HTTP 碰 Core"，评测器也遵守同一条
边界，否则 fake 与 live 两条路径的行为会悄悄分叉。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cadence.runtime.errors import ErrorCode, ToolError

from .dataset import Case


class ResetNotAllowed(RuntimeError):
    """目标 Core 里还有数据，而调用方没有明确允许清空。

    这是评测器最危险的一步：`reset()` 会通过 API 把**所有可见任务**删掉。
    一条 `--core live` 命令就能清空演示库，所以默认拒绝，必须显式 `--allow-reset`。
    """


@dataclass
class SeedResult:
    aliases: dict[str, str] = field(default_factory=dict)
    versions: dict[str, int] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)


class CoreSandbox:
    """对 FakeCore / CoreClient 都成立的最小沙箱接口。"""

    def __init__(
        self,
        core: Any,
        *,
        label: str = "fake",
        allow_reset: bool = False,
        snapshot_dir: Path | None = None,
    ) -> None:
        self.core = core
        self.label = label
        self.allow_reset = allow_reset
        self.snapshot_dir = snapshot_dir
        self.reset_count = 0

    # ------------------------------------------------------------------

    async def reset(self) -> None:
        """清空所有可见任务。最多 100 批，避免 Core 异常时死循环。

        **live 模式下目标库非空时默认拒绝**（见 `ResetNotAllowed`）：
        fake 模式无所谓，live 模式一条命令就能删光演示数据。
        允许清空时，若给了 `snapshot_dir`，会先落一份 `pre-reset-*.json`。
        """
        self.reset_count += 1

        first = await self.list_tasks()
        if first and self.label == "live" and not self.allow_reset:
            more = "+" if len(first) >= 50 else ""
            raise ResetNotAllowed(
                f"目标 Core 里还有 {len(first)}{more} 条可见任务，reset 会通过 API "
                "把它们全部删掉。\n"
                "确认要清空：加 --allow-reset（建议同时加 "
                "--snapshot-dir .tmp/eval-snapshots 先留一份备份）。"
            )

        dumped: list[dict[str, Any]] = []
        batches = 0
        while batches < 100:
            batches += 1
            items = await self.list_tasks()
            if not items:
                break
            dumped.extend(items)
            for task in items:
                await self._delete(task["id"])
            if len(items) < 50:
                break
        else:
            raise RuntimeError("reset 超过 100 批仍未清空，Core 可能没有真正删除任务")

        if self.snapshot_dir is not None and dumped:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.snapshot_dir / f"pre-reset-{datetime.now():%Y%m%d-%H%M%S}.json"
            path.write_text(json.dumps(dumped, ensure_ascii=False, indent=2), encoding="utf-8")

    async def list_tasks(self, **params: Any) -> list[dict[str, Any]]:
        query = {"limit": 50, **params}
        data = await self.core.request("GET", "/api/v1/tasks", params=query)
        if isinstance(data, dict):
            return list(data.get("items") or [])
        return list(data or [])

    async def _delete(self, task_id: str) -> None:
        path = f"/api/v1/tasks/{task_id}"
        try:
            await self.core.request("DELETE", path)
            return
        except ToolError as exc:
            if exc.code is not ErrorCode.CONFIRM_REQUIRED:
                raise
            token = (exc.details or {}).get("confirmToken")
            if not token:
                raise
            await self.core.request("DELETE", path, confirm_token=token)

    # ------------------------------------------------------------------

    async def seed(self, case: Case) -> SeedResult:
        """按 `setup.tasks` 建任务，再走状态流转与依赖 API。"""
        result = SeedResult()
        for spec in case.tasks:
            title = str(spec.get("title") or "").strip()
            if not title:
                continue
            body: dict[str, Any] = {
                "title": title,
                "priority": str(spec.get("priority") or "NORMAL").upper(),
                "tags": list(spec.get("tags") or []),
            }
            for source, target in (
                ("description", "description"),
                ("start_at", "startAt"),
                ("due_at", "dueAt"),
                ("estimate_min", "estimateMin"),
                ("rrule", "rrule"),
                ("pinned", "pinned"),
            ):
                if spec.get(source) is not None:
                    body[target] = spec[source]

            created = await self.core.request(
                "POST",
                "/api/v1/tasks",
                body=body,
                idempotency_key=f"eval-seed-{case.id}-{title}",
            )
            created = copy.deepcopy(created or {})
            task_id = str(created.get("id"))
            alias = str(spec.get("alias") or title)
            result.aliases[alias] = task_id
            result.versions[alias] = int(created.get("version") or 0)
            result.tasks.append(self._view(created))

        # 状态流转放在全部创建之后：:complete / :start 需要真实 id，且互相独立。
        for spec in case.tasks:
            alias = str(spec.get("alias") or spec.get("title") or "")
            status = str(spec.get("status") or "TODO").upper()
            if alias not in result.aliases or status == "TODO":
                continue
            updated = await self._transition(result.aliases[alias], status, result.versions[alias])
            result.versions[alias] = int(updated.get("version") or result.versions[alias])
            for index, task in enumerate(result.tasks):
                if task.get("id") == result.aliases[alias]:
                    result.tasks[index] = self._view(updated)

        # 依赖最后连：被依赖的任务必须已经存在。
        for spec in case.tasks:
            alias = str(spec.get("alias") or spec.get("title") or "")
            parent = result.aliases.get(alias)
            depends = spec.get("depends_on")
            if not parent or not depends:
                continue
            for dependency in depends if isinstance(depends, list) else [depends]:
                dependency_id = result.aliases.get(str(dependency).lstrip("$"))
                if not dependency_id:
                    continue
                await self.core.request(
                    "POST",
                    f"/api/v1/tasks/{parent}/dependencies",
                    body={"dependsOnId": dependency_id},
                )
        return result

    async def _transition(self, task_id: str, status: str, version: int) -> dict[str, Any]:
        action = {
            "DONE": "complete",
            "DOING": "start",
            "CANCELLED": "cancel",
            "TODO": "reopen",
        }.get(status)
        if action is None:
            raise ValueError(f"未知 seed 状态：{status}")
        return await self.core.request(
            "POST",
            f"/api/v1/tasks/{task_id}:{action}",
            body={"version": version},
        )

    # ------------------------------------------------------------------

    async def snapshot(self) -> list[dict[str, Any]]:
        return [self._view(task) for task in await self.list_tasks()]

    async def task_count(self) -> int:
        return len(await self.snapshot())

    @staticmethod
    def _view(task: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": task.get("id"),
            "title": task.get("title"),
            "status": str(task.get("status") or "").upper(),
            "priority": str(task.get("priority") or "").upper(),
            "startAt": task.get("startAt"),
            "dueAt": task.get("dueAt"),
            "estimateMin": task.get("estimateMin"),
            "parentId": task.get("parentId"),
            "rrule": task.get("rrule"),
            "tags": list(task.get("tags") or []),
            "version": int(task.get("version") or 0),
        }


__all__ = ["CoreSandbox", "SeedResult"]
