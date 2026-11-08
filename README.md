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

```powershell
# 1. 起数据库
docker compose up -d postgres

# 2. 起后端（同时托管前端页面）
cd services/cadence-core
mvn spring-boot:run

# 3. 打开任务看板
Start-Process http://localhost:18080        # → /ui/index.html

# 4. 起 Agent（另开一个终端）
cd services/cadence-agent
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m uvicorn cadence.main:app --port 18000
```

> 不想起后端也能看界面：`http://localhost:18080/ui/index.html?demo`（内存演示数据）。
> 前端说明见 [`services/cadence-web/README.md`](services/cadence-web/README.md)。
>
> 没有 LLM key 时 Agent 自动进 **fake 模式**：离线、零成本、确定性，整条链路照样能跑。

```powershell
# Agent 冒烟
curl http://localhost:18000/health

# 其余开发脚本
python scripts/gen_tools.py      # 生成工具模型（改 tools.json 后必跑）
python scripts/check_matrix.py   # 校验可追溯矩阵
.\scripts\dev.ps1 agent          # 起 Agent
```

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
- **cadence-web（前端页面）**：已完成，由后端托管在 `/ui/`。
- **SP2 · cadence-agent**：**已完成**。5 个端点、12 个工具（暴露 10 个）、9 类失败各有测试、
  124 个测试全绿、guard 覆盖率 98%。真实 DeepSeek 已跑通；
  **fast 路径的延迟/成本目标未达标**（原因和数字见 [README §7](services/cadence-agent/README.md)）。
- **SP3 / SP4**：未开始。`brain/` 是桩，`plan.reschedule` 已注册但不对模型暴露。
