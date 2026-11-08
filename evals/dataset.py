"""数据集加载与结构校验（L4-1）。

数据集是 YAML（PyYAML 是**唯一**新增依赖）。如果环境装不上 PyYAML，
`load_dataset` 也能读同目录下的 `cases.json` —— 但绝不手写 YAML 解析器。

一条用例的冻结结构（`schema.json` 是给人/CI 看的同一份约束）：

    id / group / input / setup.tasks / expect.tool_calls /
    expect.final_state / expect.must_not_call / tags

`$别名`（如 `$牛奶`）不是真实 id：runner 在开跑前通过 Core API 建任务，
拿到真实 id 后用 `resolve_case` 把期望值里的别名替换掉。
`$version.牛奶` 同理，替换成 seed 后的真实 version。
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

GROUPS: tuple[str, ...] = (
    "single",
    "multi",
    "ambiguous",
    "adversarial",
    "domain",
    "regression",
)

GROUP_FILES: tuple[str, ...] = GROUPS

REQUIRED_CASE_KEYS: tuple[str, ...] = (
    "id",
    "group",
    "input",
    "setup",
    "expect",
    "tags",
)

REQUIRED_EXPECT_KEYS: tuple[str, ...] = (
    "tool_calls",
    "final_state",
    "must_not_call",
)


@dataclass
class Expect:
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_state: dict[str, Any] = field(default_factory=dict)
    must_not_call: list[Any] = field(default_factory=list)


@dataclass
class Case:
    id: str
    group: str
    input: str
    setup: dict[str, Any]
    expect: Expect
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    @property
    def tasks(self) -> list[dict[str, Any]]:
        return list(self.setup.get("tasks") or [])


@dataclass
class Dataset:
    version: str
    cases: list[Case]
    path: Path

    @property
    def group_counts(self) -> dict[str, int]:
        counts = Counter(c.group for c in self.cases)
        return {group: counts.get(group, 0) for group in GROUPS}

    def by_id(self, case_id: str) -> Case | None:
        return next((c for c in self.cases if c.id == case_id), None)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def load_dataset(path: str | Path) -> Dataset:
    """加载 `evals/datasets/v1/`（或单个 YAML/JSON 文件）。"""
    root = Path(path).resolve()
    files = _dataset_files(root)
    if not files:
        raise FileNotFoundError(f"数据集目录里没有 single/multi/... 用例文件：{root}")

    version = root.name if root.is_dir() else root.parent.name
    raw_cases: list[dict[str, Any]] = []
    seen: set[str] = set()

    for file in files:
        payload = _load_file(file)
        if payload is None:
            continue
        if isinstance(payload, dict):
            version = str(payload.get("version") or version)
            payload = payload.get("cases") or []
        if not isinstance(payload, list):
            raise ValueError(f"{file.name}: 顶层必须是用例列表或 {{version, cases}}")

        for item in payload:
            validate_case(item)
            case_id = str(item["id"])
            if case_id in seen:
                raise ValueError(f"用例 id 重复：{case_id}（{file.name}）")
            seen.add(case_id)
            record = copy.deepcopy(item)
            record.setdefault("tags", [])
            raw_cases.append(record)

    cases = [_to_case(record, source=root.name) for record in raw_cases]
    if not cases:
        raise ValueError(f"数据集为空：{root}")
    return Dataset(version=version, cases=cases, path=root)


def _dataset_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]

    files: list[Path] = []
    for stem in GROUP_FILES:
        for suffix in (".yaml", ".yml", ".json"):
            candidate = root / f"{stem}{suffix}"
            if candidate.exists():
                files.append(candidate)
                break

    # 允许额外文件（例如 cases.json / meta.yaml），但 schema.json 不是用例
    for candidate in sorted(root.iterdir()):
        if candidate in files or not candidate.is_file():
            continue
        if candidate.suffix.lower() not in {".yaml", ".yml", ".json"}:
            continue
        if candidate.name in {"schema.json", "meta.yaml", "meta.yml"}:
            continue
        files.append(candidate)
    return files


def _load_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:  # pragma: no cover - 环境问题，不是逻辑分支
        raise RuntimeError(
            "读取 YAML 数据集需要 pyyaml。装不上时请改用 JSON 数据集"
            "（同名 .json，结构完全一致），不要手写 YAML 解析器。"
        ) from exc
    return yaml.safe_load(text)


def _to_case(record: dict[str, Any], *, source: str) -> Case:
    expect_raw = record["expect"]
    return Case(
        id=str(record["id"]),
        group=str(record["group"]),
        input=str(record["input"]),
        setup=copy.deepcopy(record["setup"]),
        expect=Expect(
            tool_calls=copy.deepcopy(expect_raw["tool_calls"]),
            final_state=copy.deepcopy(expect_raw["final_state"]),
            must_not_call=copy.deepcopy(expect_raw["must_not_call"]),
        ),
        tags=list(record.get("tags") or []),
        raw=copy.deepcopy(record),
        source=source,
    )


# ---------------------------------------------------------------------------
# 校验（不引 jsonschema：只做结构校验，schema.json 保持同步文档）
# ---------------------------------------------------------------------------


def validate_case(data: Any) -> None:
    """结构不对就抛 `ValueError` —— 冻结 spec 的 `test_dataset_schema_rejects_bad_case` 依赖它。"""
    if not isinstance(data, dict):
        raise ValueError(f"用例必须是对象，实际是 {type(data).__name__}")

    missing = [key for key in REQUIRED_CASE_KEYS if key not in data]
    if missing:
        raise ValueError(f"用例缺字段：{', '.join(missing)}")

    if not str(data.get("id") or "").strip():
        raise ValueError("用例 id 不能为空")
    if data.get("group") not in GROUPS:
        raise ValueError(f"未知分组 {data.get('group')!r}，只允许 {', '.join(GROUPS)}")
    if not str(data.get("input") or "").strip():
        raise ValueError(f"{data.get('id')}: input 不能为空")

    setup = data.get("setup")
    if not isinstance(setup, dict) or not isinstance(setup.get("tasks"), list):
        raise ValueError(f"{data.get('id')}: setup.tasks 必须是列表")
    for index, task in enumerate(setup["tasks"]):
        if not isinstance(task, dict) or not str(task.get("title") or "").strip():
            raise ValueError(f"{data.get('id')}: setup.tasks[{index}] 缺 title")

    expect = data.get("expect")
    if not isinstance(expect, dict):
        raise ValueError(f"{data.get('id')}: expect 必须是对象")
    missing_expect = [key for key in REQUIRED_EXPECT_KEYS if key not in expect]
    if missing_expect:
        raise ValueError(f"{data.get('id')}: expect 缺字段：{', '.join(missing_expect)}")
    if not isinstance(expect["tool_calls"], list):
        raise ValueError(f"{data.get('id')}: expect.tool_calls 必须是列表")
    if not isinstance(expect["final_state"], dict):
        raise ValueError(f"{data.get('id')}: expect.final_state 必须是对象")
    if not isinstance(expect["must_not_call"], list):
        raise ValueError(f"{data.get('id')}: expect.must_not_call 必须是列表")

    for index, call in enumerate(expect["tool_calls"]):
        if not isinstance(call, dict) or not str(call.get("name") or "").strip():
            raise ValueError(f"{data.get('id')}: expect.tool_calls[{index}] 缺 name")
        if "args" in call and not isinstance(call["args"], dict):
            raise ValueError(f"{data.get('id')}: expect.tool_calls[{index}].args 必须是对象")

    if not isinstance(data.get("tags"), list):
        raise ValueError(f"{data.get('id')}: tags 必须是列表")


# ---------------------------------------------------------------------------
# 别名解析
# ---------------------------------------------------------------------------


def resolve_case(case: Case, aliases: dict[str, str], versions: dict[str, int]) -> dict[str, Any]:
    """把用例里的 `$别名` / `$version.别名` 换成真实值。

    返回 `record`（与 YAML 同形），runner 用它构造 fake 剧本与三类断言。
    """
    record = copy.deepcopy(case.raw)
    expect = record["expect"]
    expect["tool_calls"] = resolve_value(expect["tool_calls"], aliases, versions)
    expect["final_state"] = resolve_value(expect["final_state"], aliases, versions)
    expect["must_not_call"] = resolve_value(expect["must_not_call"], aliases, versions)
    record["tags"] = list(record.get("tags") or [])
    return record


def resolve_value(value: Any, aliases: dict[str, str], versions: dict[str, int]) -> Any:
    if isinstance(value, str):
        if value.startswith("$version."):
            key = value[len("$version.") :]
            if key not in versions:
                raise KeyError(f"未知 version 别名：{value}（已 seed：{sorted(versions)}）")
            return versions[key]
        if value.startswith("$") and len(value) > 1:
            key = value[1:]
            if key in aliases:
                return aliases[key]
            raise KeyError(f"未知任务别名：{value}（已 seed：{sorted(aliases)}）")
        return value
    if isinstance(value, dict):
        return {key: resolve_value(item, aliases, versions) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_value(item, aliases, versions) for item in value]
    return value


__all__ = [
    "GROUPS",
    "Case",
    "Dataset",
    "Expect",
    "load_dataset",
    "resolve_case",
    "resolve_value",
    "validate_case",
]
