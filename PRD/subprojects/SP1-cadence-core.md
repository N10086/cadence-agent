# SP1 · cadence-core（传统任务后端）

> **优先级 P0 ｜ 计划 24h（W1 spike 6h + W2 10h + W3 8h）｜ 语言 Java 17 + Spring Boot 3**

---

## 1. 概况

### 1.1 这是什么

一个**不智能的、普通的、任何公司都有的**任务管理后端。它有 REST 接口、有关系数据库、有乐观锁、有审计日志、有重复规则。

它不认识"帮我挪一下"是什么意思。它只会执行结构化指令。

这正是它存在的意义：**它是"AI 赋能"的那个"传统"，是 Baseline，是参照物。** 没有它，上层 Agent 就没有可对比的落差，面试官也无从判断你到底给系统增加了什么。

### 1.2 为什么单独成项

| 理由 | 说明 |
|---|---|
| **叙事需要** | "我给一个存量 Java 系统接入了 Agent" 比 "我写了个 Python 脚本调 LLM" 值钱得多 |
| **技术栈补齐** | AI 应用岗也要能看懂企业后端。Java 是国内后端最大公约数 |
| **真实验证** | Agent 需要一个"有脾气"的下游：会返回 409、会校验依赖、会拒绝非法状态流转。太顺的后端测不出 Agent 的韧性 |
| **可独立演示** | 这一层做完就已经是一个合格的后端项目，可单独写进简历 |

### 1.3 不做会怎样

上层的所有"智能"都会变成在内存 list 上跑的通配玩具。评测里的 `final_state` 断言将毫无意义，因为你没有真实的状态机。

---

## 2. 范围

### In Scope
- 任务 CRUD + 状态机 + 优先级
- 乐观锁并发控制
- 软删除 + 审计日志
- 重复任务（RRULE 子集）
- 任务依赖 + 循环检测
- 时间冲突检测
- 忙闲查询（工作时间配置）
- OpenAPI 3.1 导出
- 批量更新（事务性）

### Out of Scope
- 用户体系（`user_id` 固定为 1）
- 附件、评论、子任务树超过 2 层
- 真实的日历同步（Google/Outlook）——用本地配置表模拟
- 通知推送
- 全文检索（用 `LIKE` 足够）

---

## 3. 需求

### 3.1 功能需求

| ID | 需求 | 验收要点 |
|---|---|---|
| **FR-1** | 任务的创建/查询/局部更新/软删除 | 更新必须带 `version`，不匹配返回 409 |
| **FR-2** | 状态机：`TODO → DOING → DONE`，任意状态可 `CANCELLED` | 非法流转返回 422 并说明原因（如 DONE 不能直接回 DOING，需 reopen） |
| **FR-3** | 优先级四档：low/normal/high/urgent | 存储为枚举，排序稳定 |
| **FR-4** | 软删除 | `deleted_at` 非空即视为删除；查询默认过滤；删除需 `X-Confirm-Token` |
| **FR-5** | 乐观锁 | 同一任务并发更新，第二个请求必须 409，不允许静默覆盖 |
| **FR-6** | 重复任务 | 支持 `FREQ=DAILY/WEEKLY/MONTHLY`、`INTERVAL`、`COUNT`、`UNTIL`、`BYDAY`。`GET /occurrences?from&to` 展开为具体时间点 |
| **FR-7** | 任务依赖 | 支持 `FINISH_TO_START`；A 依赖 B 时，B 未完成不允许 A 进入 DONE；创建依赖时检测环 |
| **FR-8** | 冲突检测 | 创建/更新时，若与同用户已有任务时间重叠（在估算时长内），返回 200 + `warnings[]`（**不阻止**，只提示） |
| **FR-9** | 忙闲查询 | `GET /calendar/busy?from&to` 返回合并后的忙碌区间（工作时间之外 + 已有任务占用） |
| **FR-10** | 幂等创建 | 同一 `idempotency_key` 重复 POST 返回首次结果，不产生重复数据 |
| **FR-11** | 审计 | 所有写操作记录 `actor`、`trace_id`、`before`、`after` |
| **FR-12** | OpenAPI 导出 | `springdoc` 生成 `/v3/api-docs`，提交为 `docs/openapi.yaml` |

### 3.2 非功能需求

| ID | 需求 | 目标 |
|---|---|---|
| NFR-1 | 启动时间 | < 10s（本地） |
| NFR-2 | 单接口延迟 | p95 < 50ms（1000 条任务数据） |
| NFR-3 | 测试覆盖率 | 领域层 ≥ 70% |
| NFR-4 | 零魔法配置 | 所有外部依赖走环境变量 |
| NFR-5 | 一键启动 | `docker compose up core` 即可 |

---

## 4. 技术方案要点

### 4.1 领域模型

```
Task
├── id            UUID (字符串形式暴露给 Agent)
├── title         varchar(200)
├── description   text
├── status        enum: TODO | DOING | DONE | CANCELLED
├── priority      enum: LOW | NORMAL | HIGH | URGENT
├── due_at        timestamptz（可空）
├── start_at      timestamptz（可空）
├── estimate_min  int（可空，5-480）
├── parent_id     UUID（可空，最多 2 层）
├── pinned        boolean   ← 重排期时"不能挪"的标记
├── rrule         varchar(200)（可空）
├── tags          varchar[]（或 jsonb）
├── version       bigint    ← 乐观锁
├── deleted_at    timestamptz（可空）
└── created_at / updated_at
```

配套表：`task_dependency(task_id, depends_on_id, type)`、`task_occurrence_done(task_id, occurrence_at)`（重复任务中哪些实例已完成）、`audit_log`、`idempotency_record`。

### 4.2 三个容易做错的地方

**① 下发给 Agent 的 id 不要用自增主键。**
用 UUID 字符串。自增 id 会让模型倾向于"猜 id 是 1、2、3"，幻觉率飙升。UUID 天然不可猜，反而让模型更愿意去 search。

**② `version` 必须体现在响应里。**
Agent 的 `task.update` 需要它。如果响应里没有，模型就会编造一个，然后必然 409——这会污染评测数据，让你误以为是模型能力问题。

**③ RRULE 展开要小心时区。**
`FREQ=WEEKLY;BYDAY=MO` 在 `Asia/Shanghai` 下展开，DST（夏令时）不适用中国但有其他地区。存储统一用 `timestamptz`，展开时以任务所属时区为准。这一块至少写 8 个单测，覆盖月末（`FREQ=MONTHLY` 跨 2 月）、闰年、`UNTIL` 边界。

### 4.3 关于是否用 `ical4j`

| 方案 | 优点 | 缺点 | 建议 |
|---|---|---|---|
| `ical4j` | 标准、全 | 重、API 陡峭、文档差 | 时间充裕可选 |
| 手写子集 | 可控、可测、代码量约 300 行 | 只支持子集 | **推荐** |

手写一个 `RecurrenceExpander`，只支持 DAILY/WEEKLY/MONTHLY + INTERVAL + COUNT/UNTIL + BYDAY，**这本身就是一段能讲的代码**（面试官会问"月末 31 号怎么办"，你能答上来就赢了）。

---

## 5. 项目文件树

```
services/cadence-core/
├── pom.xml
├── Dockerfile
├── README.md
├── .env.example
└── src/
    ├── main/
    │   ├── java/io/cadence/core/
    │   │   ├── CadenceCoreApplication.java
    │   │   ├── config/
    │   │   │   ├── JacksonConfig.java          # 时间格式、null 策略
    │   │   │   ├── OpenApiConfig.java
    │   │   │   ├── ClockConfig.java            # 注入 Clock，便于测试时间
    │   │   │   └── WebConfig.java              # CORS、错误处理注册
    │   │   ├── domain/
    │   │   │   ├── Task.java
    │   │   │   ├── TaskStatus.java
    │   │   │   ├── Priority.java
    │   │   │   ├── Recurrence.java             # rrule 值对象
    │   │   │   ├── RecurrenceExpander.java     # ★ 手写展开器
    │   │   │   ├── TaskDependency.java
    │   │   │   ├── TimeWindow.java
    │   │   │   └── WorkingHours.java
    │   │   ├── repository/
    │   │   │   ├── TaskRepository.java
    │   │   │   ├── DependencyRepository.java
    │   │   │   ├── AuditRepository.java
    │   │   │   └── IdempotencyRepository.java
    │   │   ├── service/
    │   │   │   ├── TaskService.java
    │   │   │   ├── StateMachineService.java    # 状态流转校验
    │   │   │   ├── DependencyService.java      # 含环检测
    │   │   │   ├── ConflictService.java
    │   │   │   ├── BatchService.java           # 事务性批量更新
    │   │   │   └── CalendarService.java
    │   │   ├── web/
    │   │   │   ├── TaskController.java
    │   │   │   ├── CalendarController.java
    │   │   │   ├── dto/
    │   │   │   │   ├── TaskCreateRequest.java
    │   │   │   │   ├── TaskPatchRequest.java
    │   │   │   │   ├── TaskResponse.java
    │   │   │   │   ├── BatchUpdateRequest.java
    │   │   │   │   └── BusyResponse.java
    │   │   │   └── error/
    │   │   │       ├── ApiError.java           # 统一错误结构
    │   │   │       ├── ErrorCode.java
    │   │   │       └── GlobalExceptionHandler.java
    │   │   └── audit/
    │   │       └── AuditLogger.java
    │   └── resources/
    │       ├── application.yml
    │       ├── application-h2.yml
    │       ├── db/migration/
    │       │   ├── V1__init_task.sql
    │       │   ├── V2__dependency.sql
    │       │   └── V3__audit_idempotency.sql
    │       └── seed/demo.sql                   # 演示用种子数据
    └── test/java/io/cadence/core/
        ├── domain/
        │   ├── RecurrenceExpanderTest.java     # ★ 至少 8 个用例
        │   └── TimeWindowTest.java
        ├── service/
        │   ├── TaskServiceTest.java
        │   ├── DependencyCycleTest.java
        │   └── OptimisticLockTest.java         # ★ 并发测试
        └── web/
            ├── TaskControllerIT.java
            └── ContractTest.java               # 对齐 openapi.yaml
```

---

## 6. 时间路径

| 周 | 时长 | 任务 | 产出 |
|---|---|---|---|
| **W1** | 6h | Spike：`GET /tasks` + Docker Postgres + 一个测试跑通 | M0 闸门 |
| **W2 上** | 4h | 领域模型 + Flyway V1 + Repository | 表结构 |
| **W2 下** | 6h | CRUD 5 个接口 + 校验 + 统一错误 + 乐观锁 | 可用的 API |
| **W3 上** | 4h | `RecurrenceExpander` + 单测（**最费时的部分**） | FR-6 |
| **W3 中** | 2h | 依赖 + 环检测 + 冲突检测 | FR-7/8 |
| **W3 下** | 2h | 批量更新 + 审计 + openapi 导出 + 契约测试 | **M1 达成** |

### 每周检查点

- W2 结束：Postman 能跑通 5 个 CRUD 请求
- W3 中：手写一个"每月 31 号"的用例，展开结果正确
- W3 末：`curl` 触发两个并发 PATCH，一个 200 一个 409

---

## 7. 验收标准

### 7.1 功能验收（可执行）

```gherkin
场景: 乐观锁
  假设 任务 t_001 当前 version=3
  当 请求 A 用 version=3 更新标题为 "X"
  并且 请求 B 用 version=3 更新标题为 "Y"
  那么 恰好一个请求返回 200
  并且 另一个返回 409
  并且 最终数据库里标题是 200 那个的值

场景: 依赖环
  假设 已有依赖 A→B, B→C
  当 创建依赖 C→A
  那么 返回 422，错误码 DEPENDENCY_VIOLATION
  并且 数据库无新增记录

场景: 重复任务月末
  假设 任务 rrule=FREQ=MONTHLY;BYDAY=MO;BYSETPOS=-1 起始 2026-01-26
  当 展开 2026-01 到 2026-06
  那么 返回每月最后一个周一，共 6 个日期
  并且 各日期与手工计算一致

场景: 软删除
  假设 任务 t_002 已存在
  当 不带 X-Confirm-Token 调用 DELETE
  那么 返回 403 CONFIRM_REQUIRED，且任务仍在
```

### 7.2 交付检查单

- [ ] `mvn test` 全绿，覆盖率报告 ≥ 70%
- [ ] `docker compose up core` 30 秒内可用
- [ ] `docs/openapi.yaml` 已生成且被 Agent 侧成功生成客户端
- [ ] 无硬编码的模型/Agent 相关逻辑（Core 必须对 AI 一无所知）
- [ ] 1000 条种子数据下，`GET /tasks?limit=20` p95 < 50ms
- [ ] README 含 curl 示例（面试时能直接照着演示 Core 部分）

### 7.3 反向验收（更重要的"不该有"）

- [ ] Core 里搜不到 `prompt` / `llm` / `gpt` 任何字样
- [ ] 没有把业务逻辑写到 Controller 里
- [ ] 没有任何 `SELECT *` 后全表在内存里过滤的实现

---

## 8. 风险与备选

### 变体 B：Java 零基础怎么办

如果你完全没写过 Java，W1 Spike 超过 8 小时没跑通，**立即切换，不要硬扛**：

| 方案 | 差额工时 | 损失 |
|---|---|---|
| **Node + Express + Prisma** | -8h（更快） | 叙事稍弱（"Java 存量系统"变成"Node 服务"） |
| **Go + Gin + sqlc** | -4h | 生态没那么"传统"，但性能叙事好 |
| **Python + FastAPI** | -12h | ❌ **不建议**：和 Agent 同语言，就失去了"跨栈集成"的叙事价值 |

切换时保持 `openapi.yaml` 契约不变，SP2 之后**完全不受影响**。这就是先冻结契约的收益。

### 其他风险

| 风险 | 缓解 |
|---|---|
| `ical4j` 学习曲线 | 直接手写子集，300 行，可控 |
| 双数据库兼容坑 | **不做双兼容**：开发 / 测试 / 演示统一 Postgres 16，测试走 Testcontainers |
| 测试写太多拖时间 | 只要求领域层 70%，Controller 用 3 个集成测试覆盖即可 |

---

## 9. 交付物

1. 可运行的服务（Docker）
2. `docs/openapi.yaml`
3. 测试报告（覆盖率截图）
4. `seed/demo.sql`（10 个真实感任务，供演示用）
5. 一页 README，含 5 条 curl 示例
