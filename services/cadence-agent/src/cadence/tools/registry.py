"""工具注册表 + OpenAI function 定义生成。

11 个工具，不多不少。为什么是 11（PRD 05 §2.1）：
每个工具 ≈95 token，11 个 ≈1000 token，占单轮 2200 预算的 45%——这是上限；
经验上 10-15 个是甜点区，超过 15 个模型开始混淆相邻工具
（`task.update` / `task.complete` / `task.delete` 是最容易混的三兄弟）。
"""

from __future__ import annotations

from typing import Any, Iterable

from .base import Tool


def provider_name(tool_name: str) -> str:
    """把逻辑名转成 provider 允许的函数名。

    这是踩出来的：OpenAI 兼容接口要求 function name 匹配 `^[a-zA-Z0-9_-]+$`，
    **点号是不合法的**。PRD 里的 `task.search` 是逻辑名（契约、trace、评测都在用），
    发出去时换成 `task_search`，收回来再换回来。
    """
    return tool_name.replace(".", "_")


def _strip_titles(node: Any) -> Any:
    """递归删掉 pydantic 自动加的 schema 元数据 "title"。

    纯噪声：每字段一个 title，11 个工具加起来能占近三分之一 schema 体积，
    而模型从不读它。这是最便宜的省钱手段。

    但 `properties` 是一个「字段名 → schema」的映射，它下面的 key 是字段名而不是
    元数据。`task.create` / `task.split` 恰好有个字段就叫 `title`，按 key 名递归删除
    会把 required 还在、properties 里的字段删掉，provider 收到自相矛盾的 schema。
    所以这里只在非 properties 映射的位置删 title。
    """
    if isinstance(node, dict):
        stripped: dict[str, Any] = {}
        for key, value in node.items():
            if key == "title":
                continue
            if key == "properties" and isinstance(value, dict):
                # properties 的 key 是字段名，必须原样保留；只清理字段 schema 内部的元数据。
                stripped[key] = {name: _strip_titles(schema) for name, schema in value.items()}
            else:
                stripped[key] = _strip_titles(value)
        return stripped
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def tool_schema(tool: Tool, *, sanitize_name: bool = True) -> dict[str, Any]:
    """把一个 Tool 转成 OpenAI-compatible 的 function 定义。"""
    if tool.args_model is None:
        params: dict[str, Any] = {"type": "object", "properties": {}}
    else:
        params = _strip_titles(tool.args_model.model_json_schema())
        # 顶层不加 additionalProperties:false —— 模型偶尔多给一个键，
        # 我们在 validators.parse_args 里剥掉即可，不必因此让整次调用失败。
        params.setdefault("type", "object")

    fn: dict[str, Any] = {
        "name": provider_name(tool.name) if sanitize_name else tool.name,
        "description": tool.description,
        "parameters": params,
    }
    return {"type": "function", "function": fn}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._reverse: dict[str, str] = {}

    # ------------------------------------------------------------------

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool
        self._reverse[provider_name(tool.name)] = tool.name
        return tool

    def register_all(self, tools: Iterable[Tool]) -> None:
        for t in tools:
            self.register(t)

    def logical_name(self, raw: str) -> str:
        """模型回来的函数名 → 逻辑名。已经是逻辑名的原样返回。"""
        if raw in self._tools:
            return raw
        return self._reverse.get(raw, raw)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"未知工具：{name}")
        return tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return list(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def exposed(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.exposed]

    # ------------------------------------------------------------------

    def openai_tools(self) -> list[dict[str, Any]]:
        return [tool_schema(t) for t in self.exposed()]

    def schema_token_estimate(self) -> int:
        """粗估 schema 占用的 token（用于核对 2200 预算是否被撑爆）。"""
        text = "".join(str(tool_schema(t)) for t in self.exposed())
        return max(1, len(text) // 3)


def build_default_registry() -> ToolRegistry:
    """装配全部 11 个工具。这是唯一的注册入口。"""
    from . import calendar_tools, meta_tools, plan_tools, task_tools

    registry = ToolRegistry()
    registry.register_all(task_tools.TOOLS)
    registry.register_all(calendar_tools.TOOLS)
    registry.register_all(plan_tools.TOOLS)
    registry.register_all(meta_tools.TOOLS)
    return registry
