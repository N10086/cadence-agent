"""参数校验 + id 白名单。

**这是 SP2 最值钱的一个模块。**

`task_id` 不是自由参数，而是「从本轮工具返回结果里选出来的枚举值」（PRD 02 §7.2）。
把"可见 id 集合"作为每次工具调用的前置约束，
`hallucinated_id_rate` 可以直接压到 0——因为编造的 id **根本不会离开这个进程**。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from ..runtime.errors import ErrorCode, ToolError

#: 这些键的值是 task id，必须在本轮可见集合内
ID_KEYS: frozenset[str] = frozenset(
    {"task_id", "parent_id", "depends_on_id", "id", "pinned_task_ids", "ids", "depends_on"}
)


def parse_args(tool_name: str, model_cls: type[BaseModel] | None, raw: dict[str, Any]) -> BaseModel:
    """按工具自己的 pydantic 模型校验。

    校验失败 = 失败矩阵 F2（工具参数缺失）：**不当作错误，当作歧义**。
    调用方（loop）会把它转成 `meta.clarify`，让模型去问用户，而不是硬猜。
    """
    if model_cls is None:
        return BaseModel()

    def _drop_unknown(data: dict[str, Any]) -> dict[str, Any]:
        allowed = set(model_cls.model_fields.keys())
        return {k: v for k, v in data.items() if k in allowed}

    try:
        try:
            return model_cls.model_validate(raw)
        except ValidationError:
            # 模型多给字段是常事，先剥掉未知字段再试一次
            return model_cls.model_validate(_drop_unknown(raw))
    except ValidationError as exc:
        missing = [str(e["loc"][0]) for e in exc.errors() if e.get("type") == "missing"]
        invalid = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors() if e.get("type") != "missing"]
        raise ToolError(
            ErrorCode.VALIDATION_ERROR,
            f"调用 {tool_name} 的参数不完整："
            + ("缺少 " + ", ".join(missing) if missing else "")
            + ("；" if missing and invalid else "")
            + ("；".join(invalid) if invalid else ""),
            details={"tool": tool_name, "received": raw, "missing": missing},
            suggestions=[{"tool": "meta.clarify", "args": {"missing": missing}}] if missing else [],
        ) from exc


def collect_ids(args: dict[str, Any]) -> list[str]:
    """递归收集参数里所有 task id。"""
    out: list[str] = []

    def walk(value: Any, key: str | None) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, key)
        elif isinstance(value, str) and key in ID_KEYS:
            out.append(value)

    walk(args, None)
    return out


def assert_ids_visible(args: dict[str, Any], visible: set[str], *, tool_name: str) -> None:
    """白名单校验。**在发往 Core 之前**执行，编造的 id 连进程都出不去。"""
    for tid in collect_ids(args):
        if tid not in visible:
            raise ToolError(
                ErrorCode.TASK_ID_NOT_IN_CONTEXT,
                f"id {tid} 不在本轮可见任务里，不能使用。请先调用 task.search 拿到真实 id。",
                suggestions=[{"tool": "task.search", "args": {"keyword": ""}}],
                details={"tool": tool_name, "offending_id": tid, "visible": sorted(visible)[:20]},
            )


def remember_ids(visible: set[str], payload: Any) -> set[str]:
    """从工具返回结果里提取新出现的 id，扩充可见集合。

    只从**成功的工具返回**里取，且只在同一轮内累积；不跨轮持久化
    （跨轮会把"上周见过的 id"也放进来，白名单就失去意义了）。
    """
    added: set[str] = set()

    def walk(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, key)
        elif isinstance(value, str):
            if key in ("id", "taskId", "task_id") and value:
                added.add(value)

    walk(payload)
    visible |= added
    return added
