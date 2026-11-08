"""参数校验 + id 白名单。

**这是 SP2 最值钱的一个模块。**

`task_id` 不是自由参数，而是「从本轮工具返回结果里选出来的枚举值」（PRD 02 §7.2）。
把"可见 id 集合"作为每次工具调用的前置约束，
`hallucinated_id_rate` 可以直接压到 0——因为编造的 id **根本不会离开这个进程**。
"""

from __future__ import annotations

from typing import Any, Iterable

from pydantic import BaseModel, ValidationError

from ..runtime.errors import ErrorCode, ToolError

#: 这些键的值是 task id，必须在本轮可见集合内
ID_KEYS: frozenset[str] = frozenset(
    {"task_id", "parent_id", "depends_on_id", "id", "pinned_task_ids", "ids", "depends_on"}
)


def _levenshtein(a: str, b: str) -> int:
    """标准编辑距离（插入/删除/替换各计 1）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,          # 删除
                    current[j - 1] + 1,       # 插入
                    previous[j - 1] + (ca != cb),  # 替换
                )
            )
        previous = current
    return previous[-1]


def _common_prefix_len(a: str, b: str) -> int:
    length = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        length += 1
    return length


def suggest_field(name: str, allowed: Iterable[str]) -> str | None:
    """给拼错的字段名找一个最接近的合法字段。

    判定是「编辑距离 <= 2 且同前缀」，但 `due -> due_at` 这种缩写的前缀
    编辑距离是 3（补 `_at`）；它是用户最想要的那种建议，所以这里额外放行
    「一方是另一方的前缀」的情况。没有任何同前缀候选时返回 None，不瞎猜。
    """
    key = name.strip().lower()
    if not key:
        return None

    best: tuple[tuple[int, int, int], str] | None = None
    for field in allowed:
        candidate = field.lower()
        prefix = _common_prefix_len(key, candidate)
        if prefix < 2:
            continue
        distance = _levenshtein(key, candidate)
        if distance > 2 and not (candidate.startswith(key) or key.startswith(candidate)):
            continue
        rank = (distance, -prefix, len(candidate))
        if best is None or rank < best[0]:
            best = (rank, field)
    return best[1] if best else None


def parse_args(tool_name: str, model_cls: type[BaseModel] | None, raw: dict[str, Any]) -> BaseModel:
    """按工具自己的 pydantic 模型校验。

    校验失败 = 失败矩阵 F2（工具参数缺失）：**不当作错误，当作歧义**。
    调用方（loop）会把它转成 `meta.clarify`，让模型去问用户，而不是硬猜。

    **未知字段必须拒绝，不能静默丢弃**（L1-1）：模型把 `due_at` 写成 `due`
    时，若静默丢弃，任务会以「没有截止时间」创建成功，模型却以为设置成功了。
    这条直接决定 L4 的 `arg_acc` 是不是真数字。

    只检查**顶层参数**：`task.update.patch` 是 `dict[str, Any]`，里面的键
    由 Core 校验，不在这里拦——否则 `patch={"due_at": ...}` 会被误杀。
    """
    if model_cls is None:
        return BaseModel()

    # 工具 schema 对外按 alias 暴露（例如 schedule.busy 的 `from` 对应内部 `from_`），
    # 所以"未知字段"必须同时接受字段名与 alias；否则模型严格照 schema 传参反而被拒。
    allowed = sorted(
        {name for name in model_cls.model_fields}
        | {field.alias for field in model_cls.model_fields.values() if field.alias}
    )
    unknown = [key for key in raw if key not in allowed]
    if unknown:
        suggestions: list[dict[str, Any]] = []
        hints: list[str] = []
        seen: set[str] = set()
        for key in unknown:
            match = suggest_field(key, allowed)
            if match is None:
                continue
            if match not in seen:
                seen.add(match)
                suggestions.append({"field": match, "reason": f"字段 {key} 不存在"})
            hints.append(f"{key} → {match}")

        message = f"调用 {tool_name} 有无法识别的字段：{', '.join(unknown)}"
        if hints:
            message += "（是否想用 " + "、".join(hints) + "？）"
        message += f"。可用字段：{', '.join(allowed)}"
        raise ToolError(
            ErrorCode.VALIDATION_ERROR,
            message,
            details={"tool": tool_name, "received": raw, "unknown": unknown, "allowed": allowed},
            suggestions=suggestions,
        )

    try:
        return model_cls.model_validate(raw)
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
