"""
种子数据生成器。

用途：
  1. 演示时有真实感的数据（不是 3 条"买牛奶"）
  2. 求解器需要规模（30-60 条活跃任务才跑得出"移动 6 个"这种结论）
  3. 评测需要可复现的初始状态（固定 seed）

设计要点：
  - 固定随机种子，同 seed 必出同数据。这是评测可复现的前提。
  - 任务标题用真实语料，不是无意义的占位符
  - 依赖占比 12%、pin 5%、逾期 8%，对应 PRD 里说的"真实分布"
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 任务标题语料。分优先级档次，让生成的数据在语义上站得住。
# 用真实语料而不是 "Task 1 / Task 2"，否则 Agent 的意图理解评测毫无意义。
# ---------------------------------------------------------------------------

TITLES_URGENT = [
    "交季度报告", "客户演示准备", "修复线上登录问题", "提交实习申请材料",
    "DSP 实验报告截止", "面试准备：模拟面试", "论文初稿提交",
]
TITLES_HIGH = [
    "复习 Agent 基础概念", "准备项目介绍", "刷两道 SQL 题", "整理简历",
    "背 Java 集合源码", "写技术博客：Spring Boot 3 升级", "准备反问面试官的问题",
    "读完《流式系统》第 4 章", "调试 Docker 端口冲突",
]
TITLES_NORMAL = [
    "写周报", "回复邮件", "整理会议纪要", "设置数据库索引", "跑一次全量测试",
    "更新 PRD 的接口章节", "复习 Redis 缓存策略", "看论文：ReAct",
    "整理 MySQL 慢查询", "写单元测试", "修 CI 的 flaky test",
]
TITLES_LOW = [
    "买牛奶", "看论文（非紧急）", "整理桌面文件", "清理旧分支", "研究 hexo 主题",
    "给笔记本换硅脂", "读一本闲书", "整理相册",
]

TAGS = ["work", "study", "interview", "health", "life", "deep-work"]


@dataclass
class SeedTask:
    key: str                  # 生成期内部引用，不会提交给 API
    title: str
    priority: str
    status: str
    start_at: str | None
    due_at: str | None
    estimate_min: int | None
    pinned: bool
    tags: list[str]
    rrule: str | None = None
    depends_on: list[str] = field(default_factory=list)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def generate(seed: int = 42, active: int = 45, history: int = 400, now: datetime | None = None) -> dict:
    """
    生成一份可复现的种子数据。

    active  : 未来 14 天内的活跃任务数（求解器的真实规模）
    history : 历史任务数（用于测搜索、分页、性能）
    """
    rng = random.Random(seed)
    now = now or datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    today = now.replace(hour=9)

    tasks: list[SeedTask] = []
    counter = 0

    def next_key() -> str:
        nonlocal counter
        counter += 1
        return f"t{counter:04d}"

    # ---- 活跃任务 -----------------------------------------------------------
    for i in range(active):
        key = next_key()

        # 优先级分布：urgent 10% / high 30% / normal 45% / low 15%
        roll = rng.random()
        if roll < 0.10:
            priority, pool = "URGENT", TITLES_URGENT
        elif roll < 0.40:
            priority, pool = "HIGH", TITLES_HIGH
        elif roll < 0.85:
            priority, pool = "NORMAL", TITLES_NORMAL
        else:
            priority, pool = "LOW", TITLES_LOW

        title = pool[i % len(pool)]

        # 工时：p50=60, p90=240
        estimate = rng.choice([15, 30, 30, 45, 60, 60, 90, 120, 180, 240, 240, 360])

        # 排布：需求日 offset 0..14
        offset = rng.randint(0, 14)
        day = today + timedelta(days=offset)
        start = day + timedelta(hours=rng.choice([0, 1, 3, 5]))

        # 8% 逾期：需求日已经过了
        is_overdue = rng.random() < 0.08
        if is_overdue:
            start = today - timedelta(days=rng.randint(1, 5))

        # 75% 有硬截止 —— PRD 里说"逾期 8%"要先有截止期才谈得上逾期。
        # 之前设 60% 但只拿到 26/45，因为 8% 逾期的任务 start 被挪到过去，
        # 截止日也跟着前移，落在视野外的比例变高了。这里直接提到 75%。
        due = None
        if rng.random() < 0.75:
            due = start + timedelta(days=rng.randint(1, 5))
            due = due.replace(hour=23, minute=59, second=0)

        # 5% pin
        pinned = rng.random() < 0.05

        # 8% 是重复任务（站会、日报这类）。
        # 这个比例来自"每个工作日历里大约有 2-3 个固定重复项"，
        # 太高会让排期问题失真（重复任务本质上不参与重排）。
        rrule = None
        if rng.random() < 0.08:
            rrule = rng.choice([
                "FREQ=WEEKLY;BYDAY=MO,WE",
                "FREQ=WEEKLY;BYDAY=FR",
                "FREQ=DAILY;INTERVAL=2",
            ])

        tasks.append(SeedTask(
            key=key,
            title=title if rrule is None else title + "（周期）",
            priority=priority,
            status="TODO",
            start_at=_iso(start),
            due_at=_iso(due) if due else None,
            estimate_min=estimate,
            pinned=pinned,
            tags=rng.sample(TAGS, rng.choice([0, 1, 1, 2])),
            rrule=rrule,
        ))

    # ---- 依赖：在活跃任务里建立 2-3 层依赖链 ------------------------------
    # 做法：先按 start_at 排序，然后挑出 ~5 个"后继"，每个依赖一个更早的任务。
    # 不用 "12% 随机" —— 随机抽样在小样本（45 条）上方差太大，
    # 实测 seed=42 只能连上 3 条，形不成可演示的链。
    active_sorted = sorted(
        [t for t in tasks if t.status == "TODO" and t.start_at],
        key=lambda x: x.start_at,
    )
    n_dep_targets = max(4, int(len(active_sorted) * 0.12))
    # 从后半段挑后继，保证前面有可选的前置
    candidates = active_sorted[len(active_sorted) // 2:]
    for dependent in rng.sample(candidates, min(n_dep_targets, len(candidates))):
        dep_idx = active_sorted.index(dependent)
        pool = [x for j, x in enumerate(active_sorted) if j < dep_idx]
        if pool:
            dependent.depends_on.append(rng.choice(pool).key)

    # ---- 已完成的历史任务 ---------------------------------------------------
    for i in range(history):
        key = next_key()
        rng_priority = rng.choice(["LOW", "NORMAL", "NORMAL", "HIGH"])
        pool = TITLES_LOW + TITLES_NORMAL if rng_priority != "HIGH" else TITLES_HIGH

        days_ago = rng.randint(1, 120)
        when = today - timedelta(days=days_ago)
        tasks.append(SeedTask(
            key=key,
            title=rng.choice(pool),
            priority=rng_priority,
            status="DONE",
            start_at=_iso(when),
            due_at=None,
            estimate_min=rng.choice([15, 30, 60, 90]),
            pinned=False,
            tags=rng.sample(TAGS, rng.choice([0, 1])),
        ))

    # ---- 统计汇总 -----------------------------------------------------------
    def count(fn) -> int:
        return sum(1 for t in tasks if fn(t))

    summary = {
        "total": len(tasks),
        "active": active,
        "history": history,
        "with_start": count(lambda t: t.start_at is not None),
        "with_due": count(lambda t: t.due_at is not None),
        "pinned": count(lambda t: t.pinned),
        "recurring": count(lambda t: t.rrule is not None),
        "with_deps": count(lambda t: t.depends_on),
        "done": count(lambda t: t.status == "DONE"),
    }

    return {
        "meta": {
            "seed": seed,
            "generated_at": _iso(now),
            "profile": "demo",
            "timezone": "Asia/Shanghai",
        },
        "summary": summary,
        "tasks": [asdict(t) for t in tasks],
    }


def main() -> int:
    args = sys.argv[1:]
    seed = 42
    active = 45
    history = 400

    for a in args:
        if a.startswith("--seed="):
            seed = int(a.split("=", 1)[1])
        elif a.startswith("--active="):
            active = int(a.split("=", 1)[1])
        elif a.startswith("--history="):
            history = int(a.split("=", 1)[1])

    data = generate(seed=seed, active=active, history=history)

    out_dir = Path(__file__).resolve().parents[1] / "data"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"seed-demo-s{seed}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(data["summary"], ensure_ascii=False, indent=2))
    print(f"\nwritten -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
