# SP3 · cadence-brain（领域智能）

> **优先级 P1 ｜ 计划 26h（W7 9h + W8 9h + W9 8h）｜ 语言 Python（纯函数为主）**

---

## 1. 概况

### 1.1 这是什么

Agent 的"专业判断"。SP2 让它**可靠**，SP3 让它**有用**。

它回答三个问题：

1. 这句话到底要我干什么？（意图与约束解析）
2. 一个模糊的大目标应该拆成什么？（任务分解）
3. 这周剩下的时间怎么塞下这些任务？（排期求解 + 主动建议）

### 1.2 核心架构立场（本项目最重要的一条设计判断）

> **LLM 负责"翻译"，算法负责"求解"，LLM 再负责"解释"。**

三段式：

```
自然语言 ──[LLM]──► 结构化约束 ──[确定性算法]──► 可行方案 ──[LLM]──► 人话解释
```

**反面教材**（90% 的 AI 项目会这么写）：

```
自然语言 ──[LLM]──► "排好的任务列表"
```

后者的问题是致命的：

| 问题 | 后果 |
|---|---|
| 模型不会真的做组合优化 | 排出来的计划违反硬约束（两个任务同一时段、依赖倒置） |
| 每次结果不一样 | 不可复现、不可测试、用户不信任 |
| 无法解释"为什么这么排" | 只能说"AI 觉得这样比较好" |
| token 贵、慢 | 50 个任务的上下文直接爆 |
| 无法验证 | 你没有任何指标可以证明它排得对 |

前者的好处：求解器是**纯函数**，可以单测、可以 fuzz、可以证明"硬约束 100% 满足"。而模型只做它真正擅长的事——把"我周三到周五发烧"翻译成 `unavailable: [2026-11-05T00:00, 2026-11-08T00:00)`。

**这一条判断，就是这个项目在面试里最值钱的东西。** 它证明你知道"什么时候不该用 LLM"。

---

## 2. 范围

### In Scope
- 意图分类（规则优先 + 模型兜底）
- 中文相对时间解析（规则，100% 准确）
- 约束模型（不可用时段、可移动策略、pin）
- 任务分解（LLM）
- 排期求解（贪心 + 修复 + 局部搜索）
- 可行性报告（`cannot_fit`）
- 主动建议引擎（规则触发 + LLM 措辞）
- 结果解释生成

### Out of Scope
- 多目标进化算法、强化学习排程（❌ 过度设计）
- 跨用户资源调度（单用户）
- 真实日历 API 同步
- 长期偏好学习（可作为"未来工作"写在 README）

---

## 3. 需求

### 3.1 功能需求

| ID | 需求 | 验收要点 |
|---|---|---|
| **FR-1** | 意图分类：`CRUD_SINGLE` / `CRUD_MULTI` / `DECOMPOSE` / `RESCHEDULE` / `QUERY` / `CHITCHAT` / `UNCLEAR` | 规则覆盖 ≥ 70%，规则未命中走快模型；准确率 ≥ 90% |
| **FR-2** | 中文时间解析 | "今天/明天/后天/大后天/本周五/下周三/三天后/月底/下个月初"全部正确；**100%** |
| **FR-3** | 歧义检测 | 缺失槽位（目标不明、时间不明、范围不明）→ 触发 clarify |
| **FR-4** | 约束抽取 | 把"周三到周五干不了活"→ `TimeWindow`；"能挪的挪"→ `movable=all`；"别动客户演示"→ `pinned` |
| **FR-5** | 任务分解 | 输入目标 + 可用容量，输出 3-7 个子任务（标题/工时/优先级/依赖），总工时超容量时报告 |
| **FR-6** | 排期求解 | 输入任务集 + 忙闲 + 不可用窗口，输出 `moved` / `kept` / `cannot_fit`，硬约束 100% 满足 |
| **FR-7** | 目标函数 | `迟到惩罚(按优先级加权) + 改动惩罚 + 时间偏移惩罚`，权重可配 |
| **FR-8** | 可行性报告 | 无法满足的任务必须显式列出并说明原因，不允许硬塞 |
| **FR-9** | 解释生成 | 基于 diff 生成中文解释，不得出现 diff 之外的动作 |
| **FR-10** | 主动建议 | 5 类触发器；同类建议 24h 内最多 1 次；可关闭 |

### 3.2 非功能需求

| ID | 需求 | 目标 |
|---|---|---|
| NFR-1 | 求解耗时（50 任务 / 14 天） | ≤ 2s |
| NFR-2 | 求解确定性 | 同输入 → 同输出（禁随机，或固定 seed） |
| NFR-3 | 求解器测试覆盖率 | ≥ 85% |
| NFR-4 | 时间解析测试 | ≥ 40 条用例，全绿 |

---

## 4. 技术方案

### 4.1 时间解析（别交给模型）

这是最容易被"顺手交给模型"、然后翻车的地方。

```python
# brain/dates.py
RELATIVE = {
    "今天": 0, "明天": 1, "后天": 2, "大后天": 3,
}
WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3,
           "五": 4, "六": 5, "日": 6, "天": 6}

def parse(text: str, now: datetime, tz: str) -> ParsedTime | Ambiguous:
    # "下周三" → 是本周之后的那个周三（+7），而非"最近的周三"
    # "本周五" → 若已过，必须返回 Ambiguous 而不是猜下周
    # 默认时间：若只有日期无时点 → 需要判断是 due(23:59) 还是 start(09:00)
```

**关键规则**：

| 输入 | 行为 |
|---|---|
| "周五前交报告" | `due_at = 本周五 23:59`（`前` → 截止语义） |
| "周五开会" | 若本周五已过 → `Ambiguous("本周五已过，是下周五吗？")` |
| "三天后" | `now + 3 days`，**保留当前时刻** |
| "月底" | 当月最后一天 23:59 |
| 只有日期无时间 | 默认 `due_at = 当日 23:59`，并在回复中说明 |

**这 40 条用例要单独放一个测试文件**，因为这是"100% 正确率"的承诺，也是最好写测试的部分。

### 4.2 约束模型

```python
@dataclass(frozen=True)
class Constraints:
    unavailable: list[TimeWindow]      # 不可用时段
    movable: Literal["all", "low_priority_only", "none"]
    pinned: set[str]                   # 明确不能动的任务 id
    horizon: tuple[date, date]         # 排期视野
    hard_deadlines: bool = True        # due_at 是否视为硬约束

@dataclass(frozen=True)
class Problem:
    tasks: list[TaskSnapshot]          # id, title, priority, estimate_min,
                                       # due_at, pinned, depends_on, current_slot
    busy: list[TimeWindow]             # 已有占用（会议等）
    working_hours: list[TimeWindow]    # 每天 9-12, 14-18
    constraints: Constraints

@dataclass(frozen=True)
class Solution:
    placements: dict[str, TimeWindow]
    moved: list[Move]
    kept: list[str]
    cannot_fit: list[Infeasible]
    objective_value: float
    solve_ms: int
```

`Problem` 和 `Solution` 都是**不可变 dataclass**，求解器是 `solve(problem) -> Solution` 的纯函数。**没有 I/O，没有数据库，没有 LLM。** 这是它能被 fuzz 测试的前提。

### 4.3 求解算法

不用 OR-Tools（重、难以解释、杀鸡用牛刀）。用三段：

```
1. 拓扑排序（处理依赖）
2. 贪心排布（按 优先级 → 截止期 排序，逐任务找最早可行槽）
3. 局部修复（解决贪心留下的硬违反）
4. 局部搜索（在可行解空间内降低 churn，最多 200 次迭代）
```

**第 1 步：拓扑排序**
依赖关系 `A depends_on B` → B 先排。有环则直接返回 `Infeasible(reason="循环依赖")`（虽然 Core 会拦，但求解器要自己兜）。

**第 2 步：贪心**

```python
order = sorted(tasks, key=lambda t: (
    -priority_weight(t.priority),   # urgent 先
    t.due_at or FAR_FUTURE,         # 截止早的先
    t.id,                           # 稳定性：保证确定性输出
))

for t in order:
    if t.pinned:
        place_at_original(t); continue
    slot = find_earliest_feasible(t, after=dep_end(t), avoid=busy+unavailable)
    if slot is None:
        cannot_fit.append(Infeasible(t, reason="视野内无可用时段"))
    else:
        place(t, slot)
```

**第 3 步：修复**
贪心可能让高优先级任务抢了低优先级任务的唯一可用槽。修复策略：

- 若某任务 `cannot_fit`，尝试把**优先级更低**的已放置任务挤走，再看能否放下
- 每次修复记录 swap，最多 3 轮，避免抖动

**第 4 步：局部搜索（降 churn）**
目标：在保持可行性前提下，尽量减少"移动的任务数"和"移动的距离"。

```python
best = current
for _ in range(200):
    cand = neighbor(best)      # 随机选一个已移动任务，尝试挪回原位或附近
    if feasible(cand) and objective(cand) < objective(best):
        best = cand
    else:
        break_on_no_improve(patience=30)
```

**约束**：随机种子固定，保证同输入同输出。

### 4.4 目标函数

```python
def objective(sol, prob) -> float:
    lateness = sum(
        max(0, (p.end - t.due_at).hours) * priority_weight(t.priority)
        for t, p in placed(sol) if t.due_at
    )
    churn = sum(1 for m in sol.moved) * 3.0        # 移动一个任务 = 3 分
    drift = sum(abs(m.to - m.frm).hours for m in sol.moved) * 0.2
    overweight = sum(1 for i in sol.cannot_fit) * 100
    return lateness + churn + drift + overweight
```

权重写在 `objective.py` 顶部，可配置。**这个函数是"产品价值观"的代码化**——把 churn 权重调高，系统就更保守（少动）；调低就更激进。

面试时可以说："我通过调整 churn 权重，让平均移动任务数从 11 降到 6.4，同时逾期数不变。"

### 4.5 容量计算（分解的约束）

```python
def available_capacity(problem, window) -> int:
    """返回 window 内的可用分钟数"""
    total = sum(hours_in(w) for w in working_hours within window)
    return total - sum(overlap(w, window) for w in busy + unavailable)
```

分解任务时，把 `available_capacity` 作为 prompt 的一部分：

> "现在是周二，本周剩余可用时间 9.5 小时。请把目标拆成 3-7 个子任务，**总预估工时不超过 9 小时**。"

**把容量数字直接给模型，是分解可用率从 40% 提升到 80% 的关键**——模型对时间估算天然乐观，不告诉它上限它就会拆出 30 小时的工作。

### 4.6 主动建议引擎

**不要做"Agent 在后台自主思考"**——贵、不可控、难调试。

正确做法：**规则触发 + LLM 措辞**。

```python
TRIGGERS = {
    "due_soon":      lambda t, now: t.due_at and 0 < (t.due_at - now) < 24h and t.status != DONE,
    "overdue":       lambda t, now: t.due_at and (now - t.due_at) > 1 * DAY,
    "stalled":       lambda t, now: t.status == DOING and (now - t.updated_at) > 3 * DAY,
    "blocked":       lambda t, now: has_unfinished_dependency(t) and (now - t.created_at) > 2 * DAY,
    "overcommitted": lambda t, now: has_conflict(t),           # 同一时段两个任务
}
```

流程：

```
定时任务（每小时） → 跑 TRIGGERS → 得到候选信号
                  → 按 (类型, 任务) 去重、限频（24h 1 次）
                  → 按严重度排序，取 Top 1
                  → LLM 生成一句建议（或直接用模板，省钱）
                  → 存入 suggestions 表，由前端拉取
```

**产品细节（这些细节体现产品判断）**：

- 静默时段（22:00-08:00 不推）
- 用户可 dismiss，dismiss 后同类 7 天不再提
- 每条建议必须带一个"一键执行"的动作，否则就是噪音
- 每天最多 1 条

---

## 5. 项目文件树

```
services/cadence-agent/src/cadence/brain/
├── __init__.py
├── intent.py              # 意图分类（规则 + 模型兜底）
├── dates.py               # ★ 中文相对时间解析（纯规则）
├── slots.py               # 时间槽：切分、合并、相交、取最近可用
├── constraints.py         # 约束抽取（LLM + 校验）
├── capacity.py            # 容量计算
├── decompose.py           # 任务分解
├── explain.py             # 结果解释生成
├── proactive.py           # 主动建议引擎
├── triggers.py            # 5 类触发器（纯规则）
└── solver/
    ├── __init__.py
    ├── model.py           # Problem / Solution / Move / Infeasible
    ├── feasibility.py     # 可行性校验（唯一权威）
    ├── greedy.py          # 贪心排布
    ├── repair.py          # 修复 pass
    ├── local_search.py    # 局部搜索降 churn
    ├── objective.py       # 目标函数 + 权重
    └── topological.py     # 依赖排序 + 环检测
```

```
services/cadence-agent/tests/
├── unit/
│   ├── test_dates.py              # ★ 40+ 用例
│   ├── test_slots.py
│   ├── test_intent.py
│   ├── test_capacity.py
│   └── solver/
│       ├── test_feasibility.py
│       ├── test_greedy.py
│       ├── test_dependencies.py
│       ├── test_no_fit.py
│       ├── test_determinism.py    # 同输入同输出
│       └── test_property.py       # ★ hypothesis 属性测试
└── fixtures/
    ├── problems/                  # 10 个固定 Problem JSON
    └── golden/                    # 对应的期望 Solution
```

### 关于 `test_property.py`

用 `hypothesis` 生成 1000 个随机问题实例，断言：

```python
@given(random_problem())
def test_never_violates_hard_constraints(prob):
    sol = solve(prob)
    assert no_overlap(sol)
    assert all_pinned_kept(sol)
    assert dependencies_satisfied(sol)
    assert all(slot in working_hours for slot in sol.placements.values())
```

**这是整个项目里技术含量最高的 20 行代码。** 面试时提一句"我用属性测试跑了 1000 个随机实例，硬约束违反率为 0"，比讲十个功能都有用。

---

## 6. 时间路径

| 周 | 时长 | 任务 | 产出 |
|---|---|---|---|
| **W7 上** | 4h | `dates.py` + 40 条测试 | FR-2 达标 |
| **W7 中** | 2h | `intent.py`（规则 + 快模型兜底） | FR-1 |
| **W7 下** | 3h | `constraints.py` + 歧义检测 | FR-3/4 |
| **W8 上** | 4h | `solver/model.py` + `feasibility.py` + `topological.py` | 骨架可跑 |
| **W8 中** | 3h | `greedy.py` + `repair.py` | 能出方案 |
| **W8 下** | 2h | `local_search.py` + `objective.py` | churn 下降 |
| **W9 上** | 3h | `decompose.py` | FR-5 |
| **W9 中** | 2h | `explain.py` + `proactive.py` | FR-9/10 |
| **W9 下** | 3h | 属性测试 + golden 测试 + 与 SP2 联调 | **M3 达成** |

### 闸门

- **W7 末**：时间解析 40 条用例全绿。不绿不许进 W8。
- **W8 末**：`solve()` 能在 2s 内处理 50 个任务，且 `test_determinism` 通过。

---

## 7. 验收标准

### 7.1 求解器验收（最硬的部分）

```gherkin
场景: 病假重排
  假设 有 14 个任务分布在本周和下周
  并且 用户周三到周五不可用
  并且 "客户演示" 被 pin
  当 请求重排，horizon=14 天
  那么 所有硬约束满足
  并且 "客户演示" 时间未变
  并且 moved 数量 ≤ 暴力全量顺延的 60%
  并且 至少列出 1 个 cannot_fit（若确实排不下）
  并且 求解耗时 < 2s

场景: 确定性
  当 同一个 Problem 连续调用 solve() 10 次
  那么 10 次返回的 placements 完全一致

场景: 依赖倒置
  假设 A 依赖 B，B 需要 3h
  当 排布
  那么 A 的开始时间 ≥ B 的结束时间

场景: 无解要诚实
  假设 一个任务需要 8h，但整个视野内只剩 2h
  当 排布
  那么 该任务出现在 cannot_fit
  并且 不出现在 placements
```

### 7.2 属性测试验收

- [ ] `hypothesis` 跑 1000 个随机实例，硬约束违反数 = 0
- [ ] 所有实例求解耗时 < 3s（超时视为失败）
- [ ] 求解器覆盖率 ≥ 85%
- [ ] 不存在 `random` 未设种子的调用

### 7.3 分解验收

- [ ] 10 个模糊目标（"准备面试"/"写毕业论文"/"搬家"）分解结果均 ≤ 7 项
- [ ] 每项都有 `estimate_min`
- [ ] 总工时 ≤ 给定容量（超了必须重新分解或降级）
- [ ] 用户拒绝时不落库

### 7.4 反向验收

- [ ] `solver/` 目录下搜不到 `llm`、`openai`、`http` 任何字样
- [ ] `dates.py` 里搜不到任何模型调用
- [ ] 求解器没有全局可变状态

---

## 8. 风险与备选

| 风险 | 概率 | 缓解 |
|---|---|---|
| 贪心 + 修复写出来的解质量差 | 中 | 先保证可行（硬约束），质量差可接受；churn 优化作为增强项 |
| 局部搜索拖慢启动 | 低 | 迭代上限 200，patience 30，可一键关闭 |
| 中文时间解析覆盖不全 | 中 | 只承诺覆盖 40 条明确用例；超出范围返回 `Ambiguous` 而不是猜——**这本身就是正确行为** |
| 分解质量不稳定 | 高 | 给模型容量数字 + few-shot；结果必须经过容量校验，不合格就重试 1 次再降级 |
| 主动建议变成噪音 | 中 | 严格限频 + 必须先做用户测试（自己用一周） |

---

## 9. 交付物

1. `solver/` 纯函数库（可独立 import 使用）
2. 40+ 条时间解析测试
3. 属性测试报告（1000 实例，0 违反）
4. 10 个固定 Problem 的 golden solution
5. `docs/solver.md`：算法说明 + 为什么不用 OR-Tools + 目标函数设计 trade-off
6. 一张 churn 权重 vs 移动任务数的实验曲线（**这张图很能打**）
