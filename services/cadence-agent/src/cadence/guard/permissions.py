"""工具权限矩阵。

单用户系统也要有最小权限思维（PRD 02 §8）：
模型不能碰数据库，破坏性操作必须两阶段，**不存在"执行任意 SQL"这类工具**。

这个模块只做"这个动作允不允许、要不要确认"，不碰业务。
"""

from __future__ import annotations

from enum import Enum


class Permission(str, Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    CONTROL = "control"


#: 每个工具允许的权限（改这里等于改产品的安全边界，review 时要醒目）
TOOL_PERMISSIONS: dict[str, Permission] = {
    "task.search": Permission.READ,
    "schedule.busy": Permission.READ,
    "task.create": Permission.WRITE,
    "task.update": Permission.WRITE,
    "task.start": Permission.WRITE,
    "task.complete": Permission.WRITE,
    "task.split": Permission.WRITE,
    "task.delete": Permission.DESTRUCTIVE,
    "plan.reschedule": Permission.READ,
    "plan.apply": Permission.DESTRUCTIVE,
    "meta.clarify": Permission.CONTROL,
    "meta.finish": Permission.CONTROL,
}

#: 明确禁止存在的工具（反向验收：grep 到这六个名字出现在 registry 里就是 bug）
FORBIDDEN_TOOLS: tuple[str, ...] = (
    "sql.execute",
    "http.fetch",
    "shell",
    "task.bulk_delete",
    "user.impersonate",
    "config.write",
)


class PermissionDenied(Exception):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(message)
        self.tool = tool


def permission_of(tool_name: str) -> Permission:
    return TOOL_PERMISSIONS.get(tool_name, Permission.DESTRUCTIVE)


def is_forbidden(tool_name: str) -> bool:
    return tool_name in FORBIDDEN_TOOLS


def needs_confirm(tool_name: str, *, affected: int = 1, batch_threshold: int = 3) -> bool:
    """破坏性操作一律要确认；批量影响超过阈值也要。

    注意 `task.split` 不在破坏性里（它只新建，不改已有数据），
    但一次影响 >3 条时要确认 —— 阈值是「用户还能一眼看完」的界限。
    """
    perm = permission_of(tool_name)
    if perm is Permission.DESTRUCTIVE:
        return True
    if affected > batch_threshold:
        return True
    return False


def assert_executable(tool_name: str) -> None:
    if is_forbidden(tool_name):
        raise PermissionDenied(tool_name, f"工具 {tool_name} 被明确禁止，不在允许清单内")
