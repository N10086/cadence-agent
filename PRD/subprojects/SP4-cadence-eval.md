# SP4 · cadence-eval（评测与可观测）

> **优先级 P0 ｜ 计划 22h（W4 3h + W9 2h + W10 10h + 零散 7h）｜ 语言 Python**

---

## 1. 概况

### 1.1 这是什么

项目的**记分牌和显微镜**。

- 记分牌：125 条用例，跑一次知道当前能力边界在哪
- 显微镜：每条 trace 都能回放，知道失败发生在哪一步、为什么

### 1.2 为什么它的编号在 SP3 之前

因为**它必须先于功能存在**。

初学者最容易犯的错：花了 8 周做出一个 Agent，第 9 周想加评测，结果发现自己根本不知道"好"是什么标准，只能凭感觉写几条"看起来对"的用例。然后改 prompt 全靠玄学。

正确的顺序：**先定义"什么算成功"，再去做**。

### 1.3 它在面试中的价值

这是最能体现"工程师 vs 爱好者"差别的子项目。

一个能说清下面这几句话的候选人，和"我做了个 AI 待办"的候选人，不在一个层级：

> "我把 125 条用例分成 6 组，用 4 个版本做消融。加了 id 白名单之后，幻觉率从 12% 降到 0，代价是 p95 延迟增加 0.6 秒。双模型路由让 p50 降到 1.6 秒，但 p95 上升到 4.2 秒——因为复杂请求走了 DeepSeek。我认为这个 trade-off 是值的，因为……"

这段话里的每一个数字，都来自 SP4。

---

## 2. 范围

### In Scope
- 数据集（YAML）与加载器
- 沙箱执行（每条用例独立、可回滚）
- 指标计算（13 个）
- 多版本对比（消融实验）
- 报告生成（Markdown + HTML）
- Trace 查看器（Web）
- 结果缓存（省钱）
- CI 集成（smoke 集）

### Out of Scope
- LLM-as-judge 的复杂体系（只用于 1 个指标）
- 自动生成测试用例（人工构造更可靠）
- 分布式压测
- 线上流量回放（没有线上流量）

---

## 3. 需求

### 3.1 功能需求

| ID | 需求 | 验收要点 |
|---|---|---|
| **FR-1** | YAML 数据集，支持 6 个分组 | 加载后 schema 校验通过 |
| **FR-2** | 每组用例在独立沙箱执行（独立 DB 事务 / 独立 Core 实例） | 用例之间零污染 |
| **FR-3** | 断言三类：`tool_calls` / `final_state` / `must_not_call` | 三类全过才算 pass |
| **FR-4** | 13 个指标自动计算 | 见 `04-metrics.md` |
| **FR-5** | 消融实验：同一数据集跑多套配置 | 输出对比表 |
| **FR-6** | 报告：Markdown（进仓库）+ HTML（好看） | 含失败用例 Top 5 与分组聚合 |
| **FR-7** | 结果缓存：`(model, prompt_hash, input)` → 输出 | 二次跑同集，API 调用量降 ≥ 80% |
| **FR-8** | 单条用例可单独重跑：`--case multi-007` | 调试用 |
| **FR-9** | Trace 查看器：时间线、展开每步、显示 token/耗时/成本 | 单文件 Web，无需后端 |
| **FR-10** | CI：PR 触发 smoke（20 条），< 3min | 失败阻塞合并（或至少警告） |
| **FR-11** | 分组聚合，定位薄弱环节 | 输出每组通过率 |

### 3.2 非功能需求

| ID | 需求 | 目标 |
|---|---|---|
| NFR-1 | smoke 集耗时 | ≤ 3min |
| NFR-2 | full 集耗时 | ≤ 15min |
| NFR-3 | full 集成本 | ≤ ¥1.5 |
| NFR-4 | 可复现 | 同数据集 + 同模型版本 → 结果一致（缓存命中时 100%） |
| NFR-5 | 零外部依赖 | 不依赖任何 SaaS 平台 |

---

## 4. 技术方案

### 4.1 沙箱策略（三种，选最省事的）

| 方案 | 隔离度 | 成本 | 建议 |
|---|---|---|---|
| 每案例前重置 DB（`TRUNCATE` + seed） | 中 | 每条 +50ms | **推荐** |
| 每条用例一个事务，结束回滚 | 高 | 几乎为 0 | 需要 Core 配合 |
| 每条用例起一个 Core 容器 | 最高 | 5s/条 | ❌ 太慢 |

推荐第一种：Core 提供一个仅测试环境可用的 `POST /_test/reset` 接口（生产禁用）。

> **注意**：Agent 侧也要重置（会话状态、缓存、可见 id 集合），否则上一条用例的上下文会污染下一条——这是评测里最常见的 bug。

### 4.2 数据集组织

```
evals/
├── datasets/
│   ├── v1/
│   │   ├── single.yaml        # 45 条
│   │   ├── multi.yaml         # 25 条
│   │   ├── ambiguous.yaml     # 20 条
│   │   ├── adversarial.yaml   # 15 条
│   │   ├── domain.yaml        # 12 条
│   │   ├── regression.yaml    # 8 条
│   │   └── meta.yaml          # 版本、作者、变更说明
│   └── schema.json            # JSON Schema，CI 校验
├── configs/
│   ├── b0_bare.yaml           # 裸调用
│   ├── b1_contract.yaml       # + 工具契约
│   ├── b2_guard.yaml          # + 护栏
│   └── b3_routing.yaml        # + 双模型路由（完整版）
├── reports/
│   └── 2026-01-15-full.md
└── cache/                      # gitignore
```

### 4.3 四个消融版本怎么实现

不改代码，只改**配置开关**：

```yaml
# b0_bare.yaml
name: B0 裸调用
features:
  tool_contract: false      # 用简版 schema
  guard_id_whitelist: false
  guard_confirm: false
  clarify_enabled: false
  model_routing: false      # 全部走 fast
```

```python
def build_agent(config: EvalConfig) -> Agent:
    a = Agent(core_client)
    if not config.tool_contract: a.use_minimal_schema()
    if config.guard_id_whitelist: a.guard.enable_whitelist()
    ...
    return a
```

> **这是 SP4 的设计难点**：为了让消融可行，SP2 的护栏必须是**可开关的**，而不是写死在链路里。写 SP2 的时候就要预留这些开关。

### 4.4 指标计算

```python
@dataclass
class CaseResult:
    case_id: str
    group: str
    passed: bool
    tool_calls_expected: list[ToolCall]
    tool_calls_actual: list[ToolCall]
    final_state_ok: bool
    must_not_call_violated: bool
    latency_ms: int
    cost: Decimal
    trace_id: str
    error: str | None
```

指标从 `list[CaseResult]` 聚合。所有指标必须是**确定性计算**，不接受 LLM 判断（除了 `explanation_faithfulness`）。

### 4.5 成本控制（零预算的命门）

| 手段 | 效果 |
|---|---|
| 结果缓存（文件级，hash key） | 二次跑省 90% |
| smoke 集走 `fast` 模型 | 成本 ≈ 0 |
| full 集仅在里程碑跑 | 每周最多 2 次 |
| 失败用例优先重跑，全量每周 1 次 | 省 75% |
| 缓存命中率写入报告头 | 便于判断数据可信度 |

**开发期的标准做法**：改完 prompt → 跑 `--case` 单条 → 跑 `--group multi` → 全量。
不要每次都跑全量。

### 4.6 Trace 查看器

单文件 HTML + 一个 Python 脚本把 JSONL 转成数据：

```
┌─ Trace t_8f2a  (总计 3.42s, ¥0.0041) ────────────────┐
│ 00:00.000  USER    "我这周发烧了…"                    │
│ 00:00.120  LLM     glm-4-flash · classify · 340ms    │
│            → intent=RESCHEDULE                       │
│ 00:00.480  LLM     glm-4-flash · extract · 520ms     │
│            → {unavailable: [...], movable: "all"}    │
│ 00:01.020  TOOL    schedule.busy · 88ms              │
│            ← 7 windows                               │
│ 00:01.110  TOOL    plan.reschedule · 1120ms          │
│            ← moved=6 kept=7 cannot_fit=1             │
│ 00:02.240  GUARD   batch size 6 > 3 → CONFIRM_REQUIRED│
│ 00:02.250  LLM     glm-4-flash · explain · 610ms     │
│ 00:02.870  DONE    "我排了一个方案，需要你确认…"      │
└──────────────────────────────────────────────────────┘
```

**这个界面在面试演示时极其有效**——它把"Agent 在想什么"变成了可视的东西。

---

## 5. 项目文件树

```
evals/
├── pyproject.toml
├── README.md
├── datasets/
│   ├── schema.json
│   └── v1/
│       ├── single.yaml
│       ├── multi.yaml
│       ├── ambiguous.yaml
│       ├── adversarial.yaml
│       ├── domain.yaml
│       ├── regression.yaml
│       └── meta.yaml
├── configs/
│   ├── b0_bare.yaml
│   ├── b1_contract.yaml
│   ├── b2_guard.yaml
│   └── b3_routing.yaml
├── src/cadence_eval/
│   ├── cli.py                 # typer: run / compare / report / trace
│   ├── loader.py              # 数据集加载 + schema 校验
│   ├── sandbox.py             # 环境重置
│   ├── runner.py              # 执行引擎（并发 + 重试 + 缓存）
│   ├── assertions.py          # 三类断言
│   ├── metrics.py             # 13 个指标
│   ├── compare.py             # 消融对比
│   ├── cache.py               # 结果缓存
│   ├── report/
│   │   ├── markdown.py
│   │   ├── html.py
│   │   └── templates/report.html.j2
│   └── trace_view/
│       ├── build.py           # JSONL → 静态 HTML
│       └── template.html
├── reports/
│   └── .gitkeep
├── traces/                     # gitignore
└── .github/
    └── workflows/
        └── eval-smoke.yml
```

---

## 6. 时间路径

| 周 | 时长 | 任务 | 产出 |
|---|---|---|---|
| **W4** | 3h | 数据集 schema + 手写 20 条 single 用例 | 格式定稿 |
| **W5-W8 每周末** | 每周 1h | 补 10-15 条用例（**不集中做**，边用边补） | 逐步到 125 条 |
| **W9 下** | 2h | 先把 runner 跑通，出第一版数字 | 哪怕只有 60 条 |
| **W10 上** | 4h | `metrics.py` + 报告生成 | 第一份报告 |
| **W10 中** | 3h | 四个消融配置 + 对比表 | **核心产出** |
| **W10 下** | 3h | trace 查看器 + CI | 可演示 |
| **W11** | 3h | 补 adversarial 组 + 失败用例复盘 | 报告定稿 |

### 关键原则：数据集是**长出来的**，不是一次性写的

每周用系统的时候，遇到一次失败就当场记一条用例。12 周下来自然到 125 条，而且每条都是真问题。

集中花 10 小时写 125 条用例的结果通常是：写得慢、质量差、一半用例根本没测到真问题。

---

## 7. 验收标准

### 7.1 功能验收

```gherkin
场景: 单条调试
  当 执行 eval run --case multi-012 --config b3_routing
  那么 输出该用例的三类断言结果
  并且 输出 trace_id，可用 trace 查看器打开

场景: 消融对比
  当 执行 eval compare --configs b0,b1,b2,b3 --suite full
  那么 生成一张 4 行 × 6 列的对比表
  并且 每行标注该配置相对上一版的变化百分比
  并且 报告头记录数据集版本与模型版本

场景: 缓存
  当 连续执行两次相同的 full 跑
  那么 第二次的 API 调用次数 < 第一次的 20%
  并且 两次的通过率完全一致

场景: 隔离
  当 执行 single 组全部 45 条
  那么 任意两条用例之间，数据库状态与会话状态均不残留
```

### 7.2 交付检查单

- [ ] 125 条用例，6 个分组，全部分布合理
- [ ] `eval run --suite smoke` 在 CI 中 < 3min 通过
- [ ] 报告含真实数字（不是占位符）
- [ ] 四版本对比表 + 一段结论文本（解释差异）
- [ ] trace 查看器能打开任意 trace 并正确渲染
- [ ] 缓存命中率在报告头显示
- [ ] `reports/` 里至少有 4 份不同日期的报告（证明迭代过）

### 7.3 反向验收

- [ ] 用例里没有"含糊断言"（比如只检查"返回了文本"）
- [ ] 没有任何用例依赖执行顺序
- [ ] 没有把评测代码写进生产路径（`evals/` 与 `services/` 完全解耦）
- [ ] 没有用 LLM 判断工具调用是否正确

---

## 8. 风险与备选

| 风险 | 概率 | 缓解 |
|---|---|---|
| 用例标注太耗时 | **高** | 每周只补 10 条，利用真实使用记录；绝不在 W10 集中写 |
| full 集太慢/太贵 | 中 | 并发执行（4 并发）+ 缓存；smoke 常跑，full 每周 1 次 |
| 消融实验需要改 SP2 代码 | 中 | **写 SP2 时就预留 feature flag**，这是 SP2 的隐含需求 |
| 指标定义与实现不一致 | 中 | 指标定义以 `04-metrics.md` 为准，代码里每个指标函数注释引用对应章节 |
| 报告没人看 | 低 | 报告放进 README 首屏，每次更新截一张图 |

---

## 9. 交付物

1. 125 条数据集（YAML，进仓库）
2. `eval` CLI（run / compare / report / trace）
3. 4 份不同日期的评测报告
4. 消融对比表 + 结论段
5. Trace 查看器（单文件 HTML）
6. CI workflow
7. `docs/eval-method.md`：方法论说明（数据集怎么来的、指标怎么算的、为什么信这些数字）
