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
| 求解 | 交给 LLM | **交给确定性算法，LLM 只做翻译** |
| 失败处理 | 无 | 错误码分类 + 自修复 + 降级 |
| 危险操作 | 直接执行 | dry-run + confirm_token |
| 评测 | 无 | 数据集 + 基线对比 + CI |
| 可观测 | 无 | 全链路 trace + 成本/延迟归因 |

一句话：**它替代的不是"录入"，是"计划维护"这个认知环节。**

---

## 仓库结构

```
specs/                 规格层（SDD 的真相来源）
├── contracts/         OpenAPI + 工具 schema，机器可读
├── requirements/      FR 编号的验收规格
├── adr/               架构决策记录
└── matrix.md          FR → 实现 → 测试（脚本生成）

services/
├── cadence-core/      Java 17+ / Spring Boot 3 —— 传统系统
├── cadence-agent/     Python 3.11 / FastAPI —— Agent 运行时
└── cadence-web/       单页演示界面

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

> fake 只验证评测流水线；真实数字用 `--llm live --core live` 跑，报告头会标 `live`。
> 不想起后端也能看界面：`http://localhost:18080/ui/index.html?demo`（内存演示数据，
> 对话区也走内存假事件，不连 Agent）。

---

## 已知限制

- **单 worker**：会话锁、SSE 缓冲都在进程内，多副本需要会话粘滞或外部存储，本版不做。
- **单 provider**：只有 DeepSeek；L1 "换模型"实际是换 fast 提示（同 provider）。
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
  124 个测试全绿、guard 覆盖率 98%。真实 DeepSeek 已跑通；
  **fast 路径的延迟/成本目标未达标**（原因和数字见 [README §7](services/cadence-agent/README.md)）。
- **cadence-web（前端）**：L5 完成。任务板 / 对话区（SSE 7 事件）/ Trace 抽屉 / 确认 diff 三组 /
  `?demo` 内存演示；无 React/Vue/构建链。
- **SP4 · 评测**：L4/L4.5 完成。35 条数据集、13 指标、六表报告、`task.start` 与 no-op 不变量；
  fake 流水线 35/35（fake 数字不是能力结论）。
- **SP3**：未开始。`brain/` 是桩，`plan.reschedule` 已注册但不对模型暴露。
