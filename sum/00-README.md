# SP1 阶段总结 · cadence-core

> 阶段：W1 – W3（传统任务后端）
> 状态：**已完成**，FR-1 ~ FR-12 全部交付
> 产出：48 个源文件 / 2289 行主代码 / 1551 行测试代码 / **86 个测试全绿**

---

## 一、这个阶段到底做了什么

一句话：**造了一个"完全不认识 AI"的传统 Java 后端。**

它提供 14 个 REST 端点，管任务的生命周期、依赖、重复规则、冲突、幂等和审计。
它里面搜不到 `prompt`、`llm`、`gpt` 任何一个词 —— 这是刻意守住的边界。

**为什么必须这样**：整个项目的叙事是"给一个存量系统加 AI"。如果 Core 自己就带着 AI 逻辑，
那这个项目就退化成"一个用 AI 写的小工具"，而不是"一个传统系统 + 一个 AI 层"。
面试官看的是后者。

---

## 二、数字（都是跑出来的，不是估的）

| 指标 | 值 |
|---|---|
| 主代码 | 48 个文件 / 2289 行 |
| 测试代码 | 14 个文件 / 1551 行 |
| 测试用例 | **86 个，0 失败 0 错误** |
| 测试类 | 11 个（单元 2 / 服务集成 6 / Web 契约 3） |
| REST 端点 | 14 个 |
| 数据库表 | 5 张（task / task_dependency / audit_log / idempotency_record / flyway_schema_history） |
| Flyway 迁移 | 3 个（V1 任务表 / V2 依赖 / V3 审计+幂等） |
| 端到端冒烟 | 34 项，全过 |
| 种子数据规模 | 445 条（45 活跃 + 400 历史） |

### 测试分布

```
RecurrenceTest                21   RRULE 展开器（纯单元）
TaskStateMachineTest          13   状态机（集成）
IdempotencyAndConfirmTest     10   幂等 + 确认令牌（集成）
Sp1CompletionIT                9   HTTP 契约
AuditServiceTest               7   审计（集成）
ConflictServiceTest            7   冲突检测（集成）
DependencyServiceTest          7   依赖 + 环检测（集成）
BatchServiceTest               5   批量事务（集成）
ContractConformanceIT          3   契约双向一致性
OpenApiExportIT                3   springdoc 导出
CadenceCoreApplicationTests    1   上下文加载
```

**测试代码 : 主代码 = 0.68 : 1**。对一个后端来说这个比例偏高，是有意的 —— PRD 里 SP1 的验收标准写了
"领域层覆盖率 ≥ 70%"，而覆盖率的唯一保证就是先写够测试。

---

## 三、交付的功能清单（FR-1 ~ FR-12）

| FR | 需求 | 实现方式 | 关键验收 |
|---|---|---|---|
| FR-1 | CRUD + 软删除 | JPA + `deleted_at` | 已删除任务查询 404 |
| FR-2 | 状态机 | `TaskStateMachine` 独立类 | 非法流转 422 |
| FR-3 | 优先级四档 | 枚举 + 排序 | 排序稳定 |
| FR-4 | 删除需确认 | `ConfirmTokenService` 两阶段 | 无令牌 403，令牌单次有效 |
| FR-5 | 乐观锁 | `@Version` + 显式版本前置校验 | 冲突 409 + `recoverable:true` |
| FR-6 | 重复规则 | 手写 `Recurrence`，不用 ical4j | 月末场景 21 条单测 |
| FR-7 | 依赖 + 环检测 | 复合主键表 + 迭代 DFS | 成环 422 |
| FR-8 | 冲突检测 | `ConflictService` + `warnings[]` | **写入成功但带提示** |
| FR-9 | 忙闲查询 | 区间合并算法 | 相接的区间合并为一段 |
| FR-10 | 幂等创建 | `X-Idempotency-Key` + 24h TTL | 同键只建一条 |
| FR-11 | 审计 | `AuditLog` + before/after JSON 快照 | 同一事务，回滚不留假账 |
| FR-12 | OpenAPI | springdoc 导出 + 手写契约双向校验 | CI 断言一致 |

---

## 四、14 个端点

```
# 任务
GET    /api/v1/tasks                     查询（6 种过滤 + hasMore/total）
POST   /api/v1/tasks                     创建（支持幂等键）
GET    /api/v1/tasks/{id}                详情（含依赖 + 冲突）
PATCH  /api/v1/tasks/{id}                局部更新（返回 warnings）
DELETE /api/v1/tasks/{id}                软删除（需确认令牌）

# 状态流转
POST   /api/v1/tasks/{id}:complete       完成（幂等 + 前置校验）
POST   /api/v1/tasks/{id}:start          开始
POST   /api/v1/tasks/{id}:reopen         恢复为 TODO
POST   /api/v1/tasks/{id}:cancel         取消

# 批量
POST   /api/v1/tasks:batch               批量更新（事务性）

# 依赖
GET    /api/v1/tasks/{id}/dependencies   查前置
POST   /api/v1/tasks/{id}/dependencies   加依赖（环检测）
DELETE /api/v1/tasks/{id}/dependencies/{dependsOnId}

# 其他
GET    /api/v1/tasks/{id}/occurrences    RRULE 展开
GET    /api/v1/tasks/{id}/history        审计历史
GET    /api/v1/calendar/busy             忙闲
```

> 注：`{id}:complete` 这类路径是 Google AIP 风格的动词后缀。
> 在 Spring MVC 里能否生效有讲究，详见 `03-问题与解决.md` 的 #5。

---

## 五、面试时怎么讲这一段

### 电梯版（30 秒）

> "我先做了一个**完全不认识 AI 的** Spring Boot 后端：14 个端点，带乐观锁、状态机、依赖环检测、
> RRULE 展开、幂等创建和审计日志。86 个测试全绿。
>
> 它的价值不在功能，在于**它是后面 AI 层的对照物和地基** —— 面试官能一眼看出传统实现是什么样，
> 才能判断我后面加的到底是什么。"

### 关键数字（背下来）

| 说法 | 数字 |
|---|---|
| 代码量 | 2289 行主代码 + 1551 行测试 |
| 测试 | 86 个用例，0 失败 |
| 端点 | 14 个 |
| 覆盖率要求 | 领域层 ≥ 70% |

### 被追问时的三个硬点

**① "你怎么保证这个后端是可靠的？"**
→ 86 个测试，分三层：纯单元（RRULE 21 条）、服务集成（Testcontainers 跑真 Postgres）、
Web 契约（MockMvc 断言状态码和错误结构）。不用 H2 —— 因为 H2 对 `timestamptz`/`text[]`/`jsonb`
支持不完整，测试和生产的差异会藏 bug。

**② "为什么手写 RRULE 不用 ical4j？"**
→ 三个理由：库太重、我只需要子集、**这段逻辑本身就是要能讲清楚的**。
面试官爱问"每月 31 号遇到 2 月怎么办"，我能当场打开 `Recurrence.java` 第 190 行给他看。

**③ "删除为什么还要确认令牌？"**
→ 因为 Core 的调用方是 Agent，而 Agent 是最容易误触删除的东西。
两阶段：第一次返回 403 + 令牌 + 任务快照，带令牌再来才真删。令牌 5 分钟、单次有效。

---

## 六、这个阶段留下的已知不足

诚实地说，有三处：

**① 审计只覆盖任务，不覆盖依赖和批量操作**
`task_dependency` 的增删没有进 `audit_log`。批量更新只记了每条的 UPDATE，没有批次级别的记录。
**影响**：查"这批数据是谁什么时候改的"时拼接不完整。
**是否需要修**：W3 之后如果要做回放，需要补。

**② 确认令牌是单机内存实现**
`ConfirmTokenService` 用 `ConcurrentHashMap`。多实例部署会失效。
**影响**：单机无影响，PRD 的 Non-Goals 里已声明不做多实例。

**③ 忙闲查询只算任务占用**
没有接入工作日历（9-12 / 14-18）、会议、通勤。
**影响**：求解器的输入还不完整，SP3 实现时要从 Agent 侧补一份 `working_hours` 配置。

---

## 七、目录

```
sum/
├── 00-README.md            本文件：总览
├── 01-架构与设计.md        分层、职责、契约、数据模型、关键设计决策
├── 02-实施记录.md          按时间线的实际执行过程，含每一步的验证结果
├── 03-问题与解决.md        ★ 18 个问题的完整记录（现象→根因→解决→教训）
├── 04-面试素材.md          ★ 面试问答、简历 bullet、可现场演示的东西
└── 05-下一步.md            交接给 SP2 的输入
```
