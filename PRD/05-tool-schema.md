# 05 · 工具契约设计

> 工具层是 Agent 与业务系统之间唯一的接触面。**这里的每一个设计错误，都会在评测里变成一串失败用例。**

---

## 1. 七条设计原则

### P1 · 工具的返回值是给模型读的，不是给日志读的
返回 JSON 必须精简。列表默认截断到 20 条并返回 `has_more: true`，不要一次塞 200 个任务进上下文。

### P2 · id 不能由模型自由生成
模型只能从**本轮可见集合**中选 id。工具 schema 里把 `task_id` 标注为"必须来自本轮 `task.search` 的结果"，guard 层做白名单校验。

### P3 · 写操作必须幂等
所有创建类工具接受 `idempotency_key`（默认由 `trace_id + step_index` 生成）。重试不会产生重复任务。

### P4 · 错误是给模型看的，必须可行动
错误返回结构统一为：

```json
{
  "ok": false,
  "error": {
    "code": "TASK_ID_NOT_FOUND",
    "message": "id t_999 不存在",
    "recoverable": true,
    "suggestions": [
      {"tool": "task.search", "args": {"keyword": "报告"}}
    ]
  }
}
```

有 `suggestions` 的模型自修复率显著高于只有 message 的。

### P5 · 一个工具只做一件事
`task.create` 不负责建依赖，`task.split` 也不负责排期。组合由 Agent 的循环完成——**这正是 Agent 存在的意义**。

### P6 · 破坏性操作 `dry_run` 优先
任何影响 >1 条数据的操作，必须先支持 `dry_run=true` 返回预估影响，再 `apply`。

### P7 · 工具描述里写"禁忌"
比写"能做什么"更有用。例如：

> `task.delete`：删除任务。**禁止**在未确认的情况下调用。**禁止**批量删除。若用户说"清理一下"，必须先 `task.search` 列出候选并要求确认。

---

## 2. 工具清单

| # | 名称 | 类型 | 权限 | 需要确认 |
|---|---|---|---|---|
| 1 | `task.search` | 读 | `read` | 否 |
| 2 | `task.create` | 写 | `write` | 否（单条） |
| 3 | `task.update` | 写 | `write` | 否（单条） |
| 4 | `task.complete` | 写 | `write` | 否 |
| 5 | `schedule.busy` | 读 | `read` | 否 |
| 6 | `task.split` | 批量写 | `write` | 是（>3 条） |
| 7 | `task.delete` | 破坏性 | `destructive` | **是** |
| 8 | `plan.reschedule` | 计算 | `read` + `dry_run` | 否（纯计算） |
| 9 | `plan.apply` | 破坏性 | `destructive` | **是** |
| 10 | `meta.clarify` | 控制流 | — | 否 |
| 11 | `meta.finish` | 控制流 | — | 否 |

> `meta.clarify` / `meta.finish` 是**控制流工具**，不是业务工具。它们的价值是把"提问"和"结束"变成模型必须显式做出的选择，从而可以被评测统计。

### 2.1 为什么是 11 个

**边际成本**：每个工具约 55 token schema + 40 token 描述 ≈ **95 token**。11 个 ≈ 1000 token，占单轮 2200 预算的 45%——这是上限。

**选择准确率随数量衰减**：经验上 10-15 个是甜点区，超过 15 个模型开始混淆相邻工具（`task.update` / `task.complete` / `task.delete` 是最容易混的三兄弟）。

**所以砍掉了两个原本想加的**：

| 原本想加 | 实际做法 | 理由 |
|---|---|---|
| `task.list` | 合并进 `task.search`（无过滤条件即 list） | 两工具语义重叠，模型会纠结用哪个 |
| `task.get` | `task.search` 支持按 `id` 查询 | 同上；少一个工具 = 少一类误选 |

### 2.2 明确禁止存在的工具

```
✗ sql.execute       ✗ http.fetch        ✗ shell
✗ task.bulk_delete  ✗ user.impersonate  ✗ config.write
```

**"不做什么"比"做什么"更能体现设计。** 面试被问"你怎么防模型乱来"，答案不是 prompt，是**它压根没有可乱来的工具**。

---

## 3. 详细 schema

### 3.1 `task.search`

```python
class TaskSearchArgs(BaseModel):
    keyword: str | None = Field(None, description="标题/描述关键词，模糊匹配")
    status: Literal["todo", "doing", "done", "cancelled"] | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    due_before: datetime | None = Field(None, description="ISO8601，含时区")
    due_after: datetime | None = None
    limit: int = Field(10, le=20)

# 返回
{
  "ok": true,
  "items": [
    {"id": "t_001", "title": "买牛奶", "status": "todo",
     "priority": "normal", "due_at": "2026-11-05T10:00:00+08:00"}
  ],
  "has_more": false,
  "total": 1
}
```

**注意**：返回值里只给模型判断需要的字段，不给 `created_at`、`version`、`description` 全文。

---

### 3.2 `task.create`

```python
class TaskCreateArgs(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    priority: Literal["low","normal","high","urgent"] = "normal"
    due_at: datetime | None = None
    estimate_min: int | None = Field(None, ge=5, le=480)
    parent_id: str | None = Field(None, description="父任务 id，用于子任务")
    tags: list[str] = []
    idempotency_key: str | None = None
```

**禁忌**：不要在同一次调用里创建多个任务（应调多次，让模型看到每一步结果）。

---

### 3.3 `task.update`

```python
class TaskUpdateArgs(BaseModel):
    task_id: str
    version: int = Field(..., description="乐观锁版本号，来自 search 结果")
    patch: dict = Field(..., description="只包含要改的字段")
```

**注意 `version`**：这是让"并发修改"可被检测的关键。模型必须从 `task.search` 拿到版本号，这条约束同时也是一个天然的幻觉检测器——编造的 id 往往配着编造的 version。

---

### 3.4 `task.split`（批量创建子任务）

```python
class TaskSplitArgs(BaseModel):
    parent_id: str | None = None
    parent_title: str | None = None      # 无父任务时就地创建
    children: list[ChildTask] = Field(..., min_length=2, max_length=7)
    dry_run: bool = True

class ChildTask(BaseModel):
    title: str
    estimate_min: int | None
    priority: Literal["low","normal","high","urgent"] = "normal"
    depends_on: list[int] | None = Field(None, description="子任务下标，表示依赖")
```

**约束**：`children` 上限 7 条。超过 7 条说明分解粒度太粗，应拒绝并要求重新分解——这条规则同时是产品质量约束和 prompt 约束。

---

### 3.5 `task.delete`

```python
class TaskDeleteArgs(BaseModel):
    task_id: str
    confirm_token: str | None = Field(None, description="首次调用必须为空")
```

两阶段：

1. 无 token → 返回 `{ok: false, error: {code: "CONFIRM_REQUIRED", confirm_token: "ct_..."}}`，附任务快照
2. 带 token → 软删除，写入审计

---

### 3.6 `plan.reschedule`（旗舰工具）

```python
class RescheduleArgs(BaseModel):
    unavailable: list[TimeWindow] = Field(
        ..., description="不可用时段，如病假 [周三00:00, 周五24:00)"
    )
    movable: Literal["all", "low_priority_only", "none"] = "all"
    pinned_task_ids: list[str] = Field(
        default_factory=list, description="用户明确要求不动的任务"
    )
    horizon_days: int = Field(14, ge=1, le=60)
    dry_run: bool = True
```

返回：

```json
{
  "ok": true,
  "plan_id": "p_8f2a",
  "summary": {"total": 14, "moved": 6, "kept": 7, "cannot_fit": 1},
  "moved": [
    {"id": "t_003", "title": "写周报", "from": "2026-11-05T14:00+08:00",
     "to": "2026-11-09T10:00+08:00", "reason": "原时段不可用，优先级normal"}
  ],
  "kept": [
    {"id": "t_007", "title": "客户演示", "reason": "已 pin，外部依赖"}
  ],
  "cannot_fit": [
    {"id": "t_011", "title": "交报告",
     "reason": "硬截止 11-07，可用时间不足 6h，无论如何都会逾期"}
  ],
  "hard_constraints_satisfied": true,
  "solve_ms": 412
}
```

**设计要点**：

- 必须返回 `cannot_fit`——诚实报告"排不下"，而不是硬塞
- `reason` 是给模型解释用的素材，不是给人看的最终文案
- `hard_constraints_satisfied: false` 时 Agent 必须拒绝 apply

---

### 3.7 `plan.apply`

```python
class PlanApplyArgs(BaseModel):
    plan_id: str
    confirm_token: str
```

服务端校验：`plan_id` 存在、未过期（5min）、`confirm_token` 匹配、预览时的任务 `version` 未变。

**任一不符 → 拒绝并返回 `PLAN_STALE`**（"你的任务在确认期间被改过了，需要重新生成方案"）。这个场景在评测里是必测项。

---

### 3.8 `meta.clarify`

```python
class ClarifyArgs(BaseModel):
    question: str = Field(..., description="一个具体、可回答的问题")
    options: list[str] | None = Field(None, max_length=4, description="可选项")
    missing: list[str] = Field(..., description="缺失的槽位名，用于统计")
```

约束：

- **一次只问一个问题**（问三个问题用户会烦，且难以评测）
- `missing` 字段用于统计"哪类信息模型最常缺失"，是改进 prompt 的数据来源

---

### 3.9 `meta.finish`

```python
class FinishArgs(BaseModel):
    message: str = Field(..., description="给用户的最终回复，中文，简洁")
    actions_taken: list[str] = Field(..., description="实际执行的动作摘要")
```

`actions_taken` 会被 guard 校验：**这里提到的每个动作，必须真的发生过**。防止模型编造"我已经帮你排好了"。

---

## 4. 错误码表

| 码 | 含义 | recoverable | Agent 应做什么 |
|---|---|---|---|
| `VALIDATION_ERROR` | 参数格式错 | ✅ | 修正参数重试（最多 1 次） |
| `TASK_ID_NOT_FOUND` | id 不存在 | ✅ | 先 `task.search` 再重试 |
| `CONFLICT` | 版本冲突 | ✅ | 重新 `task.search` 拿新版本 |
| `DEPENDENCY_VIOLATION` | 依赖环 | ❌ | 告知用户，要求调整 |
| `CONFIRM_REQUIRED` | 需确认 | ✅ | 向用户展示 diff |
| `PLAN_STALE` | 方案过期 | ✅ | 重新生成方案 |
| `HARD_CONSTRAINT_UNSATISFIABLE` | 无解 | ❌ | 报告不可行，给建议 |
| `RATE_LIMITED` | 限流 | ✅ | 退避 2s 重试 |
| `UPSTREAM_UNAVAILABLE` | Core 挂了 | ✅ | 重试 3 次后告知 |
| `BUDGET_EXCEEDED` | 步数/成本超限 | ❌ | 中止并汇报进度 |

**`recoverable=false` 的错误，Agent 不得重试**——这是防止"死循环烧钱"的最后一道闸。

---

## 5. 工具与 Core API 的映射

| 工具 | Core 接口 | 说明 |
|---|---|---|
| `task.search` | `GET /api/v1/tasks` | 参数一对一映射 |
| `task.create` | `POST /api/v1/tasks` | 注入 `idempotency_key` |
| `task.update` | `PATCH /api/v1/tasks/{id}` | 带 version |
| `task.complete` | `POST /api/v1/tasks/{id}:complete` | 幂等 |
| `task.delete` | `DELETE /api/v1/tasks/{id}` | 带 `X-Confirm-Token` |
| `task.split` | `POST /api/v1/tasks:batch` | 事务 |
| `schedule.busy` | `GET /api/v1/calendar/busy` | — |
| `plan.reschedule` | 无（**纯本地计算**） | 拉数据 → 本地求解 |
| `plan.apply` | `POST /api/v1/tasks:batch` | 带 version 校验 |

**注意 `plan.reschedule` 没有对应的 Core 接口**——这是刻意的。求解在 Python 侧完成，Core 保持"哑"。这条边界守住了，Core 才能被独立测试和复用。

---

## 6. 验收

- [ ] 11 个工具全部有 JSON Schema，且能从 Schema 生成 OpenAI-compatible function 定义
- [ ] 每个工具至少有 2 条评测用例
- [ ] 所有错误码都有对应的 Agent 处理分支和测试
- [ ] 列表类返回全部实现截断 + `has_more`
- [ ] `task.delete` / `plan.apply` 的两阶段流程有集成测试
- [ ] 工具描述里包含禁忌条款（人工 review）
