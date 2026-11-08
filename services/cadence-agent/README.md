# cadence-agent · SP2 Agent 运行时

> Python 3.13 + FastAPI ｜ 端口 18000 ｜ 依赖 `cadence-core`（18080）

**它不负责聪明（那是 SP3），它负责让聪明变得可靠。**

四件事：

| # | 职责 | 模块 |
|---|---|---|
| 1 | **循环**：把"一次调用"变成"多步决策"，有预算、能中止、可重规划 | `runtime/loop.py` |
| 2 | **工具**：把 Core 的能力翻译成模型看得懂的 schema，把返回和错误翻译回模型看得懂的话 | `tools/` |
| 3 | **护栏**：夹在模型和真实数据之间，拦幻觉 id、越权、不可逆操作 | `guard/` |
| 4 | **可观测**：每一步留痕，可回放、可归因、可评测 | `runtime/trace.py` |

---

## 1. 快速开始

```powershell
# 0) 前提：Core 在跑
docker compose up -d postgres
cd services/cadence-core; mvn spring-boot:run

# 1) 安装
cd services/cadence-agent
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install fastapi uvicorn pydantic pydantic-settings httpx
.\.venv\Scripts\python.exe -m pip install pytest pytest-asyncio pytest-cov respx

# 2) 配置：仓库根目录的 .env（见 .env.example）
#    DEEPSEEK_API_KEY=sk-...
#    没有 key 时自动进入 fake 模式，整条链路照样能跑

# 3) 启动
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m uvicorn cadence.main:app --port 18000

# 4) 试一下
curl http://localhost:18000/health
```

### 一次对话

```powershell
# 发起（202，不推流）
curl -X POST http://localhost:18000/chat -H "Content-Type: application/json" `
  -d '{\"sessionId\":\"s_1\",\"message\":\"看看我今天有什么任务\"}'
# -> {"code":0,"data":{"streamId":"st_...","traceId":"tr_...","degraded":false}}

# 订阅事件（SSE）
curl -N http://localhost:18000/stream/st_...

# 看轨迹
curl http://localhost:18000/trace/tr_...
.\.venv\Scripts\python.exe -m cadence.cli trace tr_...     # 终端时间线
```

### 两种模式

| 模式 | 触发条件 | 用途 |
|---|---|---|
| `live` | `.env` 里有 `DEEPSEEK_API_KEY` | 真实对话 |
| `fake` | 没有 key，或 `LLM_MODE=fake` | 离线开发、测试、演示。**无网络、零成本、确定性** |

`LLM_MODE=auto`（默认）按有没有 key 自动选。
**FakeLLM 是一等公民，不是测试玩具**：没 key 也能把 9 类失败、护栏、trace 全部跑通。

---

## 2. 接口

5 个端点，契约冻结在 `docs/接口文档.md` 第七节。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat` | 启动一轮，返回 `202` + `streamId`/`traceId` |
| GET | `/stream/{streamId}` | SSE 订阅，支持 `Last-Event-ID` 续传 |
| POST | `/confirm` | 确认/丢弃待执行的破坏性操作 |
| GET | `/trace/{traceId}` | 完整调用链 |
| GET | `/health` | 裸对象（不包外壳），给运维探测 |

### 为什么 `/chat` 不推流

```
✗ 连接断 → 循环被 cancel → 写操作做了一半
✓ /chat 建循环返回 stream_id；循环在后台跑，事件进 per-stream 缓冲；
  /stream/{id} 只是 tail 这个缓冲
```

代价是一个内存缓冲，收益是**网络抖动不会让数据停在半修改状态**。

### SSE 7 种事件

`token` · `tool_call` · `tool_result` · `clarify` · `confirm` · `done` · `error`

```
id: 3
event: tool_call
data: {"name":"task.search","args":{"keyword":"报告"},"step":1}
```

> **唯一不用 `{code,message,data}` 的地方。** 外壳是为"一个完整响应"设计的，
> 流式没有"完整响应"这个概念；硬套会出现"外壳能不能中途关闭"这种没有答案的问题。
> 错误用 `error` 事件表达，不用 HTTP 状态码。

---

## 3. 11 个工具（SP2 暴露 9 个）

| 工具 | Core 端点 | 权限 |
|---|---|---|
| `task.search` | `GET /api/v1/tasks` | read |
| `task.create` | `POST /api/v1/tasks` | write |
| `task.update` | `PATCH /api/v1/tasks/{id}` | write |
| `task.complete` | `POST /api/v1/tasks/{id}:complete` | write |
| `task.split` | N 次幂等创建 + 依赖 | write |
| `task.delete` | `DELETE /api/v1/tasks/{id}` | **destructive** |
| `schedule.busy` | `GET /api/v1/calendar/busy` | read |
| `meta.clarify` | — | control |
| `meta.finish` | — | control |
| ~~`plan.reschedule`~~ | 无（纯本地计算） | SP3 |
| ~~`plan.apply`~~ | `POST /api/v1/tasks:batch` | SP3 |

**未实现的工具不暴露给模型。** 模型看到就会去用，然后拿到"未实现"，
白白浪费一轮和一份 schema token。契约里保留 11 个，`openai_tools()` 只发 9 个。

### 明确不存在的工具

```
✗ sql.execute   ✗ http.fetch   ✗ shell
✗ task.bulk_delete   ✗ user.impersonate   ✗ config.write
```

> 面试被问"你怎么防模型乱来"，**答案不是 prompt，是它压根没有可乱来的工具**。

---

## 4. 循环与守卫

```
START ─► CLASSIFY ─[歧义]─► CLARIFY ─► 等用户回答
            │
            ▼
          PLAN ─► ACT ─► OBSERVE ─[还要动作]─┐
                   ▲                        │ 是
                   └────────────────────────┘
                                    │ 否
                                    ▼
                                 FINISH
```

| 状态 | 守卫 |
|---|---|
| CLASSIFY | 歧义 → 必须先问，不许猜 |
| ACT | 参数校验 + id 白名单 + 权限 + 确认令牌 + 指纹去重 |
| OBSERVE | 步数 / token / 成本预算 |
| FINISH | `actions_taken` 必须真实发生过 |

详细的状态图和每个守卫的说明见 [`docs/loop.md`](docs/loop.md)。

**两处"停下来"是本质差别**：CLARIFY（歧义）和 CONFIRM（破坏性操作）。
教程版系统不会停，所以它敢乱猜、敢乱删。

---

## 5. 失败矩阵（9 类，各有测试）

| # | 失败 | 处理 | 测试 |
|---|---|---|---|
| F1 | 模型输出非法 JSON | 重试 1 次 → 仍失败给一句人话 | `test_f1_*` |
| F2 | 工具参数缺失 | **不报错**，当作歧义 → `clarify` | `test_f2_*` |
| F3 | 工具 404 | 白名单放行后仍 404（并发删除）→ 重新 search | `test_f3_*` |
| F4 | 工具 5xx | 指数退避 3 次 → `UPSTREAM_UNAVAILABLE` | `test_f4_*` |
| F5 | 循环打转 | 指纹去重 → 中断并汇报进度 | `test_f5_*` |
| F6 | 幻觉 id | **不发往 Core**，`TASK_ID_NOT_IN_CONTEXT` | `test_f6_*` |
| F7 | 危险操作 | `confirm` 事件 → `/confirm` 才执行 | `test_f7_*` |
| F8 | 上下文超限 | 压缩历史，保留结论 | `test_f8_*` |
| F9 | 模型超时 | 重试 1 次 → 降级 fast，`degraded:true` | `test_f9_*` |

> **F3 的构造方式很讲究**：如果白名单先拦，404 永远不会发生。
> 所以测试必须"search 时可见 → 执行前被删"，这是 404 唯一真实发生的场景。

### 14 个错误码

PRD 列了 10 个，落地时补了 4 个：

| 新增 | 为什么 |
|---|---|
| `TASK_ID_NOT_IN_CONTEXT` | 白名单拦截在本地发生，不发往 Core |
| `ILLEGAL_TRANSITION` | Core 会返回 422「已完成的任务不能取消」 |
| `LOOP_DETECTED` | 指纹判定打转，与"步数超预算"要分开统计 |
| `NOT_IMPLEMENTED` | SP2 的桩必须结构化拒绝，不能 500 |

`recoverable=false` 的错误**不重试** —— 这是防止死循环烧钱的最后一道闸。

---

## 6. Trace

`data/agent/traces/<日期>/<trace_id>.jsonl`，**每次 LLM / 工具调用各一行**，
写完即落盘（进程崩了也留得下已发生的步骤）。

```json
{"seq":3,"kind":"LLM","name":"llm.step1","duration_ms":1210,"tokens_in":2776,"tokens_out":103,
 "detail":{"tier":"fast","model":"deepseek-chat","tool_calls":["task.search"]}}
{"seq":4,"kind":"TOOL","name":"task.search","duration_ms":14,"ok":true,
 "detail":{"summary":"找到 4 条，共 4 条"}}
```

`kind` 除了 `LLM` / `TOOL` / `GUARD`，**新增 `DEGRADE`**（降级、熔断、预算中止）。
没有它，SP4 算不出 `degraded_rate`。

一条真实 trace 的逐步解读见 [`TRACE.md`](TRACE.md)。

---

## 7. 实测指标（真实数字，2026-11-08）

环境：本机 Windows，DeepSeek-Chat（**没有 GLM-4-Flash key**），Core 本地。
命令：`python bench.py --rounds 3 --message "看看我今天有什么任务"`

| 指标 | 目标 | 实测 | 结论 |
|---|---|---|---|
| 延迟 p50 | ≤ 1.8s | **2.3s** | ❌ 超标 28% |
| 延迟 p95 | ≤ 4.5s | 4.2s（含一次冷启动 6.4s） | ⚠️ 勉强 |
| 单轮成本 | ≤ ¥0.01 | **¥0.013–0.015** | ❌ 超标 30–50% |
| 平均步数 | ≤ 3.5 | **2.3** | ✅ |
| 多步（mutate） | — | 5.0 步，¥0.035 | — |
| 启动 | < 3s | **0.9s** | ✅ |
| 单测覆盖率 | ≥ 65% | **77%** | ✅ |
| guard 覆盖率 | ≥ 85% | **98%** | ✅ |

### 为什么没达标（如实记录）

1. **p50 1.8s 是按 GLM-4-Flash 定的**。PRD 的 fast tier 是 GLM（0.5–1.5s/次），
   而当前只有 DeepSeek（1.1–2.2s/次）。任何一次工具调用至少要 **2 次 LLM 往返**
   （决定调工具 + 读结果后作答），所以 1.8s 在 DeepSeek 上物理不可达。
2. **¥0.01 同理**。单次请求 prompt 约 2800 token（schema ~1400 + system ~600 + 结果），
   两次就是 ¥0.013。DeepSeek 有自动前缀缓存（命中部分按 1/4 计价），
   实际账单会低于这里的估算值，但按标价算确实超了。
3. 换回 GLM-4-Flash 做 fast tier（`ZHIPU_API_KEY` + 双模型路由）是唯一能真正达标的路。

**这是一个负结果，不是 bug。** 把 provider 的物理限制写清楚，比把目标改松更诚实。

---

## 8. 测试

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests -q                 # 125 passed
.\.venv\Scripts\python.exe -m pytest tests --cov=cadence      # 覆盖率
```

> **如果跑完发现目录里多了 `pytest-of-<用户名>\`**：说明这台机器上 Python 写不了系统临时目录
> （`%TEMP%` 权限不足），`tempfile.gettempdir()` 会退回到当前工作目录。
> 把临时目录指到一个可写位置即可，测试不受影响：
>
> ```powershell
> $env:TMPDIR='E:\deskbook\TODO\.tmp'   # 该目录已在 .gitignore 里
> ```

| 层 | 文件 | 内容 |
|---|---|---|
| 单元 | `test_router` `test_budget` `test_confirm` `test_fingerprint` | 纯逻辑 |
| 单元 | `test_guard_validators` `test_injection` | 护栏（**覆盖率 98%**） |
| 单元 | `test_core_client` `test_deepseek_client` | HTTP 语义、流式拼接、错误映射 |
| 集成 | `test_loop_single` `test_loop_multi` | 单轮 / S1 多步剧本 |
| 集成 | `test_failure_matrix` | **9 类失败** |
| 集成 | `test_api` `test_trace` | 5 个端点、SSE、trace 回放 |

反向验收（手工）：

```powershell
# 1. 没有裸 httpx 直连 Core
Select-String -Path src\cadence\**\*.py -Pattern 'httpx\.' | Select-String -NotMatch 'core_client|deepseek'
# 2. 没有 except: pass
Select-String -Path src\cadence\**\*.py -Pattern 'except.*:\s*$'
# 3. 没有硬编码 key
Select-String -Path src\cadence\**\*.py -Pattern 'sk-[A-Za-z0-9]{20,}'
```

---

## 9. 与 PRD 的偏差（7 条，都已落地并记录）

| # | PRD 说的 | 实际做的 | 为什么 |
|---|---|---|---|
| 1 | 工具名 `task.search` 直接发给模型 | 发 `task_search`，收到再换回来 | **OpenAI 兼容接口要求 `^[a-zA-Z0-9_-]+$`，点号非法**。这是真机跑出来的 |
| 2 | `task.search` 返回不带 `version` | 带 `version` | 不带的话模型没法调 `update`/`complete`（PRD 05 §3.1 与 §3.3 自相矛盾） |
| 3 | `task.split` → `POST /tasks:batch` 事务 | N 次幂等创建，失败报"已建 k/n" | Core 的 batch **只支持更新**，不支持批量创建（ADR D9：不回滚、报进度） |
| 4 | 10 个错误码 | 14 个 | 见 §5 |
| 5 | `kind` 只有 LLM/TOOL/GUARD | 加 `DEGRADE` | 否则 SP4 算不出 `degraded_rate` |
| 6 | 契约测试用 `responses` | 用 `respx` + `httpx.MockTransport` | `responses` 只拦 `requests`（同步），拦不到 httpx |
| 7 | Python 3.11 | 3.13 | 本机只有 3.9/3.13；无 API 差异 |

另有 2 条**契约缺口**记在 `docs/接口文档.md`，需要 SP1 配合：

- Core 的 422 不带符号错误码，`DEPENDENCY_VIOLATION` 和 `ILLEGAL_TRANSITION` 只能靠文案猜
- `/chat` 的 `degraded` 字段有时序问题（202 返回时路由还没跑）

---

## 10. 已知限制

- **`plan.reschedule` / `plan.apply` 是桩**，SP3 才实现；工具已注册但不对模型暴露
- **意图分类是关键词规则**（`brain/intent.py`），"那个报告搞一下"这类会直接进 clarify；SP3 换成模型
- **上下文压缩是截断，不是摘要**：不花钱但会丢细节；SP3 有快模型后换真摘要
- **会话落 JSON 文件**，不引 Redis（ADR D8）；单机够用，多实例会失效
- **spike 闸门 W4 没做**：GLM tool_call 连续 10 次成功率没测（没有 GLM key），
  所以"主力模型到底用 GLM 还是 DeepSeek"这个决定仍然悬着
