"""把评测数据集的 8 条背景任务灌进 benchmark 库（通过真实 HTTP，不直连数据库）。

为什么走 HTTP 而不是 SQL：
- Core 的 4 张表由 Flyway 创建，直插会跳过乐观锁、审计、幂等；
- 评测里 final_state 校验的是 API 字段（dueAt/status/tags 等），走 API 才能保证形状一致。

用法（先让 Core 连到 benchmark 库）：

    cd services/cadence-core
    $env:DB_URL='jdbc:postgresql://localhost:5433/benchmark'
    $env:DB_USER='benchmark'; $env:DB_PASSWORD='benchmark'
    mvn spring-boot:run

    # 另开终端，仓库根目录
    .\\services\\cadence-agent\\.venv\\Scripts\\python.exe scripts\\seed_benchmark.py

脚本是幂等的：按 title 判重，已存在就跳过；结尾逐字段回读校验并打印对照表。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

CORE = os.getenv("CORE_BASE_URL", "http://localhost:18080").rstrip("/")
EXPECTED_DB = os.getenv("EXPECTED_DB", "benchmark")

# ---------------------------------------------------------------------------
# 8 条背景任务：与 evals/datasets/v1 共用 setup 逐字段一致
# 时间基准 2026-11-08 10:30 +08:00（周日）
# ---------------------------------------------------------------------------
SEED = [
    {"alias": "牛奶", "title": "买牛奶", "status": "TODO", "priority": "NORMAL",
     "due_at": None, "start_at": None, "estimate_min": None, "tags": ["生活"]},
    {"alias": "周报", "title": "写周报", "status": "TODO", "priority": "HIGH",
     "due_at": "2026-11-13T18:00:00+08:00", "start_at": None, "estimate_min": None,
     "tags": ["work"]},
    {"alias": "演示", "title": "客户演示", "status": "TODO", "priority": "URGENT",
     "due_at": "2026-11-11T14:00:00+08:00", "start_at": "2026-11-11T14:00:00+08:00",
     "estimate_min": 60, "tags": ["work"]},
    {"alias": "论文", "title": "读论文", "status": "TODO", "priority": "NORMAL",
     "due_at": None, "start_at": None, "estimate_min": None, "tags": ["学习"]},
    {"alias": "房租", "title": "交房租", "status": "TODO", "priority": "NORMAL",
     "due_at": "2026-11-30T23:59:00+08:00", "start_at": None, "estimate_min": None,
     "tags": ["生活"]},
    {"alias": "健身", "title": "健身", "status": "TODO", "priority": "LOW",
     "due_at": None, "start_at": None, "estimate_min": None, "tags": ["健康"]},
    {"alias": "修bug", "title": "修 bug #42", "status": "TODO", "priority": "HIGH",
     "due_at": "2026-11-12T18:00:00+08:00", "start_at": None, "estimate_min": None,
     "tags": ["work"], "depends_on": "论文"},
    {"alias": "旧事", "title": "旧任务", "status": "CANCELLED", "priority": "LOW",
     "due_at": None, "start_at": None, "estimate_min": None, "tags": []},
]


def _call(method, path, body=None, headers=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(CORE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Actor", "benchmark-seed")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _list_all():
    status, payload = _call("GET", "/api/v1/tasks?limit=50")
    if status != 200:
        raise SystemExit(f"列出任务失败 HTTP {status}: {payload}")
    return list((payload.get("data") or {}).get("items") or [])


def _guard_database():
    """连错库是这次操作最大的风险：写进 TODO 就污染了演示数据。"""
    status, health = _call("GET", "/actuator/health")
    if status != 200 or (health or {}).get("status") != "UP":
        raise SystemExit(f"Core 健康检查未通过：HTTP {status} {health}")
    tasks = _list_all()
    if len(tasks) > len(SEED):
        raise SystemExit(
            f"⚠️ Core 当前可见 {len(tasks)} 条任务，多于种子应有的 {len(SEED)} 条 —— "
            f"它多半还连着开发库，不是 {EXPECTED_DB}。\n"
            "请先用 DB_URL 指向 benchmark 库重启 Core 再运行本脚本。"
        )


def _task_version(task_id):
    status, payload = _call("GET", f"/api/v1/tasks/{task_id}")
    return (((payload or {}).get("data") or {}).get("task") or {}).get("version", 0)


def _create(spec):
    body = {"title": spec["title"], "priority": spec["priority"], "tags": spec["tags"]}
    if spec.get("due_at"):
        body["dueAt"] = spec["due_at"]
    if spec.get("start_at"):
        body["startAt"] = spec["start_at"]
    if spec.get("estimate_min"):
        body["estimateMin"] = spec["estimate_min"]
    status, payload = _call(
        "POST", "/api/v1/tasks", body,
        headers={"X-Idempotency-Key": f"benchmark-{spec['alias']}"},
    )
    if status not in (200, 201):
        raise SystemExit(f"创建 {spec['title']} 失败 HTTP {status}: {payload}")
    return (payload.get("data") or {}).get("id")


def main():
    _guard_database()
    existing = {t["title"]: t for t in _list_all()}
    created, skipped = {}, []

    for spec in SEED:
        if spec["title"] in existing:
            skipped.append(spec["title"])
            created[spec["alias"]] = existing[spec["title"]]["id"]
            continue
        task_id = _create(spec)
        created[spec["alias"]] = task_id
        if spec["status"] == "CANCELLED":
            _call("POST", f"/api/v1/tasks/{task_id}:cancel", {"version": _task_version(task_id)})
        time.sleep(0.01)

    for spec in SEED:
        if not spec.get("depends_on"):
            continue
        status, payload = _call(
            "POST", f"/api/v1/tasks/{created[spec['alias']]}/dependencies",
            {"dependsOnId": created[spec["depends_on"]]},
        )
        if status not in (200, 201, 204, 409):
            print(f"  ! 依赖写入失败 HTTP {status}: {payload}")

    return verify(created, skipped)


def verify(created, skipped):
    by_id = {}
    for item in _list_all():
        status, payload = _call("GET", f"/api/v1/tasks/{item['id']}")
        task = (((payload or {}).get("data") or {}).get("task")) or item
        by_id[item["id"]] = task

    problems = []
    header = ("alias", "标题", "状态", "优先级", "截止", "开始", "工时", "标签")
    print("\n" + " | ".join(header))
    print("-" * 130)
    for spec in SEED:
        task = by_id.get(created.get(spec["alias"], ""), {})
        print(" | ".join([
            spec["alias"], spec["title"], str(task.get("status")),
            str(task.get("priority")), str(task.get("dueAt") or "-"),
            str(task.get("startAt") or "-"), str(task.get("estimateMin")),
            ",".join(task.get("tags") or []) or "-",
        ]))
        checks = {
            "title": (spec["title"], task.get("title")),
            "status": (spec["status"], task.get("status")),
            "priority": (spec["priority"], task.get("priority")),
            "dueAt": (spec["due_at"], task.get("dueAt")),
            "startAt": (spec["start_at"], task.get("startAt")),
            "estimateMin": (spec["estimate_min"], task.get("estimateMin")),
            "tags": (sorted(spec["tags"]), sorted(task.get("tags") or [])),
        }
        for field, (want, got) in checks.items():
            if want != got:
                problems.append(f"{spec['alias']}.{field}: want {want!r} got {got!r}")

    print("-" * 130)
    if skipped:
        print("已存在而跳过：" + "、".join(skipped))
    if problems:
        print(f"\n[FAIL] 校验失败 {len(problems)} 项：")
        for line in problems:
            print("  -", line)
        return 1
    print(f"\n[OK] 8 条背景任务与评测数据集逐字段一致（共 {len(by_id)} 条可见任务）")
    print("下一步：跑真实 live+live 评测")
    print("  python -m evals.cli run --all --llm live --core live "
          "--core-url http://localhost:18080 --allow-reset "
          "--snapshot-dir .tmp/eval-snapshots --out evals/reports/")
    print("注意：--allow-reset 会先清空本库再按用例重灌，这对 benchmark 库是预期行为。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
