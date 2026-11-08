# Cadence

传统任务后端 + Agent 协作层。

下层的 Spring Boot 后端**不认识任何 AI**，它只提供带乐观锁的 REST 接口。
上层的 Python Agent 是**无状态的**，负责理解意图、规划、求解和解释。

两者之间只有一份 OpenAPI 契约。

---

## 这个项目和"AI 待办"的区别

| 维度 | 教程版 AI TODO | Cadence |
|---|---|---|
| 意图理解 | 单轮 function call | 多轮 + 显式澄清机制 |
| 规划 | 一次 prompt 生成 | 状态机循环 + 步数预算 |
| 求解 | 交给 LLM | **设计目标：交给确定性算法，LLM 只做翻译**（SP3 未实现，本版 `plan.*` 不暴露） |
| 失败处理 | 无 | 错误码分类 + 自修复 + 降级 |
| 危险操作 | 直接执行 | dry-run + confirm_token |
| 评测 | 无 | 35 条数据集 + 13 指标 + 六表报告（基线与 CI 本版未做） |
| 可观测 | 无 | 全链路 trace + 成本/延迟归因 |

一句话：**它替代的不是"录入"，是"计划维护"这个认知环节。**

---

## 仓库结构

```
specs/                 规格层
└── contracts/         OpenAPI 3.1 契约（机器可读）
                       （PRD 里规划的 requirements / adr / matrix 本版未建）

services/
├── cadence-core/      Java 21 / Spring Boot 4 —— 传统系统
├── cadence-agent/     Python 3.13 / FastAPI —— Agent 运行时
└── cadence-web/       单页界面（无构建）

evals/                 评测集与跑分（SP4）
scripts/               代码生成、矩阵校验、开发脚本
```

---

## 快速开始

### 方式 A：容器一键起三件套（推荐）

```powershell
docker compose up -d --build
# 页面：http://localhost:18080/ui/index.html
# Core：http://localhost:18080/actuator/health
# Agent：http://localhost:18000/health
```

> 容器里 Core 通过 `CORE_WEB_DIR=/app/web` 找前端（Dockerfile 已把 `cadence-web` 静态文件拷进去），
> Agent 的 `data/agent` 挂 `cadence_agent_data` volume。没有 LLM key 时 Agent 自动进 fake 模式。

### 方式 B：本地开发（三条启动命令）

```powershell
# 1. 起数据库
docker compose up -d postgres

# 2. 起后端（Core 同时托管前端页面）
cd services/cadence-core
mvn spring-boot:run

# 3. 起 Agent（另开一个终端）
cd services/cadence-agent
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m uvicorn cadence.main:app --port 18000
```

打开 `http://localhost:18080/ui/index.html`。Agent 默认地址是 `http://<当前主机>:18000`，
可用 `?agent=http://host:18000`、`localStorage['cadence.agentUrl']` 或页面 `<meta name="cadence-agent-url">` 覆盖。

### 评测命令（一条）

```powershell
python -m evals.cli run --all --llm fake --core fake --out evals/reports/
```

> fake 只验证评测流水线；真实数字用 `--llm live` 跑，报告头会标 `live` 或 `live+fake`。
> 不想起后端也能看界面：`http://localhost:18080/ui/index.html?demo`（内存演示数据，
> 对话区也走内存假事件，不连 Agent）。

```powershell
# 真实模型 + 内存 Core（安全：不会删任何库；工具调用、耗时、成本都是真的）
$env:EVAL_FROZEN_NOW='2026-11-08T10:30:00+08:00'
python -m evals.cli run --all --llm live --core fake --no-cache --out evals/reports/

# 完整链路（Core 也是真的）：⚠️ reset 会先删光目标库，必须显式 --allow-reset
python -m evals.cli run --all --llm live --core live --allow-reset `
  --snapshot-dir .tmp/eval-snapshots --out evals/reports/
```

> `--core live` 不加 `--allow-reset` 会被直接拒绝（`ResetNotAllowed`）。
> 不要指向开发库 `TODO`——它会删掉 445 条种子数据；建议单独建 `cadence_eval` 库。
> 加了 `--snapshot-dir` 会先把待删任务落一份 `pre-reset-*.json`。

---

## 评测结果（2026-11-08 · live + live，benchmark 库，deepseek-flash）

- 汇总报告：[`evals/reports/2026-11-08-live-flash.md`](evals/reports/2026-11-08-live-flash.md)
- **逐条逐轮详细报告**：[`evals/reports/2026-11-08-详细-deepseek-flash.md`](evals/reports/2026-11-08-详细-deepseek-flash.md)
  （每条用例含：用户行为、逐轮 reasoning/决策/行为、白名单、实际 vs 标准答案、耗时/token/微元、最终回答）
- 对比（旧的非 reasoning 别名 `deepseek-chat`）：[`2026-11-08-live-chat.md`](evals/reports/2026-11-08-live-chat.md)

| 指标 | 实测 | 目标 | 判定 |
|---|---:|---:|---|
| `task_success` | **65.7%**（23/35） | ≥ 80% | ❌ |
| `tool_name_accuracy` | 90.2% | ≥ 92% | ❌ |
| `tool_seq_f1` | 88.5% | ≥ 85% | ✅ |
| `arg_field_accuracy` | 70.3% | ≥ 88% | ❌ |
| `date_parse_accuracy` | **100%** | 100% | ✅ |
| `hallucinated_id_rate` | **0.0%** | 0% | ✅ |
| `unsafe_action_blocked_rate` | **100%** | 100% | ✅ |
| `injection_resist_rate` | **100%** | 100% | ✅ |
| `clarification_rate` | 100% | ≥ 70% | ✅ |
| `latency_p50` / `p95` | 3.3s / 8.6s | 1.8s / 4.5s | ❌ |
| `cost_per_turn` | ¥0.0200 | ≤ ¥0.01 | ❌ |

**怎么读这张表（如实说明）：**

- 模型是 **`deepseek-flash`**（provider 支持的 flash 模型；`deepseek-v4.1-flash` 会返回
  400 "supported API model names are deepseek-flash, deepseek-v4-pro"）。
  它**会返回 `reasoning_content`**，详细报告里的「思考」就是原文。
- 之前的 `deepseek-chat` 是同一个底层模型的非 reasoning 别名（响应 model 也是
  `deepseek-flash`，但不返回 reasoning），所以旧报告没有思考内容；两次跑分对比见上面链接。
- 延迟变高是 reasoning 的代价（p95 5.4s → 8.6s），`cost_per_turn` 也从 ¥0.017 涨到 ¥0.020；
  换来的是 `date_parse_accuracy` 从 75% 到 100%。
- 失败项里仍有**搜索词更具体**造成的严格比对失败（`论文`→`读论文` 等），
  以及 C5/E4 的「模型选择反问」；明细都在详细报告里带字段级 diff。

---

## 工具上下文（两级注入）

每轮 loop 开始先做一次**类别路由**，只把被选中类别的工具 schema 发给模型：

```
第一级（只喂 4 段类别摘要，约 220 token）
  read       只读：搜索/查看任务、查忙闲
  write      单条写入：新建/改字段/开始/完成/拆分
  destructive 删除（不可撤销，需确认）
  control    提问澄清 / 结束本轮（永远附加）
        ↓ 模型输出 {"categories":["read","write"]}
第二级（只发这一类工具的 schema，类别摘要不再出现）
        ↓
     主模型决策 → 工具调用
```

- 多步任务每轮重新选：第 1 步 read、第 2 步 write 是常态；
- 查询类一轮只发 4 个工具（read+control），实测首步 `tokens_in` 从 3444 降到 1413；
- 路由调用本身约 220 token、7 token 输出（用 `deepseek-chat`），比省下的 schema 便宜；
- 关掉：`.env` 设 `TOOL_CATEGORY_ROUTING=false`（回到 10 个工具全量注入，用于对比）。
- 代码：`tools/routing.py`（摘要+规则+解析）、`registry.openai_tools(categories)`、`loop._route_tools()`。

---

## 已知限制

- **单 worker**：会话锁、SSE 缓冲都在进程内，多副本需要会话粘滞或外部存储，本版不做。
- **同一个 provider 内双模型**：路由判 `reason` 用 `deepseek-flash`（返回 `reasoning_content`），
  判 `fast` 用 `deepseek-chat`（同一模型的非 reasoning 别名，更快更便宜）。
  没有 GLM，跨 provider 降级仍不可用。切换方式：改 `.env` 的
  `DEEPSEEK_MODEL` / `DEEPSEEK_FAST_MODEL` 后 `docker compose up -d --force-recreate agent`。
- **取消任务不支持**：Core 的 PATCH 不接受 status；取消走 `meta.clarify` 告知边界。
- **fake 数字不是结论**：README 与报告里的 fake 数字只证明流水线通，能力结论必须 live 跑。
- **无多用户/登录**：单用户本地系统；CORS 只对 `/api/**` 放开。


---

## 文档

- **后端架构说明（代码层面，逐文件）**：[`docs/架构说明.md`](docs/架构说明.md)
- **接口文档（前后端统一规范）**：[`docs/接口文档.md`](docs/接口文档.md)
- **Agent 循环与守卫**：[`services/cadence-agent/docs/loop.md`](services/cadence-agent/docs/loop.md)
- **一条真实 trace 的解读**：[`services/cadence-agent/TRACE.md`](services/cadence-agent/TRACE.md)
- **Agent 说明**：[`services/cadence-agent/README.md`](services/cadence-agent/README.md)
- 前端页面说明：[`services/cadence-web/README.md`](services/cadence-web/README.md)
- 产品与架构：[`PRD/00-README.md`](PRD/00-README.md)
- 实施计划：[`PRD/03-roadmap.md`](PRD/03-roadmap.md)
- 指标定义：[`PRD/04-metrics.md`](PRD/04-metrics.md)

> 所有 HTTP 接口统一返回 `{code, message, data}`：成功 `code=0`，失败 `code` 等于 HTTP 状态码。
> 两个例外：`/stream/{id}` 是 SSE，`/health` 是裸对象。
> 机器可读契约见 [`specs/contracts/openapi.yaml`](specs/contracts/openapi.yaml)。

---

## 状态

- **SP1 · cadence-core（后端）**：已完成。16 个 REST 接口，测试与冒烟脚本全绿。
- **SP2 · cadence-agent**：**已完成**。5 个端点、12 个工具（暴露 10 个）、9 类失败各有测试、
  307 条测试通过（17 xfailed 是冻结的规格证据）、guard 覆盖率 98%。真实 DeepSeek 已跑通；
  **fast 路径的延迟/成本目标未达标**（原因和数字见 [README §7](services/cadence-agent/README.md)）。
- **cadence-web（前端）**：L5 完成。任务板 / 对话区（SSE 7 事件）/ Trace 抽屉 / 确认 diff 三组 /
  `?demo` 内存演示；无 React/Vue/构建链。
- **SP4 · 评测**：L4/L4.5 完成。35 条数据集、13 指标、六表报告、`task.start` 与 no-op 不变量；
  **live 首跑 25/35（71.4%）**，数字见上表，报告在 `evals/reports/`。
- **SP3**：未开始。`brain/` 是桩，`plan.reschedule` 已注册但不对模型暴露。
- **收尾（v1.0）**：评测器的 `reset` 闸门（live 不加 `--allow-reset` 直接拒绝）、
  `.env` key 读取修复（此前 `os.getenv` 覆盖成空，导致 live 评测永远跑不起来）。
