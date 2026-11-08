"""真实模型基准：量 NFR-1/2/3（延迟 p50/p95、单轮成本）。

**这会花钱。** 默认跑 3 轮，约 ¥0.01-0.05。

用法：
    python bench.py                 # 3 轮
    python bench.py --rounds 5      # 5 轮
    python bench.py --message "把买牛奶标记完成"

前提：Core 在跑（CORE_BASE_URL），且 .env 里有 DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time

sys.path.insert(0, "src")

from fastapi.testclient import TestClient  # noqa: E402

from cadence.config import get_settings, reset_settings_cache  # noqa: E402
from cadence.deps import Runtime, set_runtime  # noqa: E402
from cadence.main import create_app  # noqa: E402

PROBES = [
    ("fast-query", "看看我今天有什么任务"),
    ("fast-mutate", "把买牛奶标记完成"),
]


def read_done(client: TestClient, stream_id: str) -> dict:
    current: dict = {}
    with client.stream("GET", f"/stream/{stream_id}") as resp:
        for line in resp.iter_lines():
            if line.startswith("event:"):
                current["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                current["data"] = json.loads(line.split(":", 1)[1].strip())
            elif line == "" and current:
                if current["event"] == "done":
                    return current["data"]
                current = {}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--message", default="")
    args = parser.parse_args()

    reset_settings_cache()
    settings = get_settings()
    if settings.resolved_llm_mode != "live":
        print("当前不是 live 模式（没有 DEEPSEEK_API_KEY？），没法量真实延迟。")
        return 1

    rt = Runtime(settings)
    set_runtime(rt)
    app = create_app()

    probes = [("custom", args.message)] if args.message else PROBES
    results: list[dict] = []

    with TestClient(app) as client:
        for name, message in probes:
            for i in range(args.rounds):
                t0 = time.perf_counter()
                resp = client.post("/chat", json={"sessionId": f"bench_{name}_{i}", "message": message})
                if resp.status_code != 202:
                    print(f"[{name}] HTTP {resp.status_code}: {resp.text[:200]}")
                    continue
                data = resp.json()["data"]
                done = read_done(client, data["streamId"])
                elapsed = (time.perf_counter() - t0) * 1000
                row = {
                    "probe": name,
                    "round": i + 1,
                    "ms": round(elapsed),
                    "steps": done.get("steps"),
                    "cost": done.get("costYuan", 0.0),
                    "degraded": done.get("degraded"),
                    "endedBy": done.get("endedBy"),
                }
                results.append(row)
                print(f"[{name} #{i + 1}] {row['ms']}ms  steps={row['steps']}  ¥{row['cost']:.4f}  {row['endedBy']}")

    if not results:
        return 1

    print("\n==== 汇总 ====")
    for name, _ in probes:
        rows = [r for r in results if r["probe"] == name]
        if not rows:
            continue
        lat = sorted(r["ms"] for r in rows)
        cost = statistics.mean(r["cost"] for r in rows)
        steps = statistics.mean(r["steps"] or 0 for r in rows)
        p50 = lat[len(lat) // 2]
        p95 = lat[max(0, int(len(lat) * 0.95) - 1)]
        print(
            f"{name:<12} n={len(rows)}  p50={p50}ms  p95={p95}ms  平均 steps={steps:.1f}  "
            f"平均成本=¥{cost:.4f}"
        )

    print("\nNFR 对照：p50 ≤ 1800ms ｜ p95 ≤ 4500ms ｜ 单轮 ≤ ¥0.01")
    set_runtime(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
