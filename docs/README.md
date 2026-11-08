# 文档索引

## Core（SP1 · Java）

| 文档 | 说明 |
|---|---|
| [架构说明.md](架构说明.md) | **后端 cadence-core 的代码级说明**：完整文件树 + 每层/每个文件干什么 + 一次请求的完整链路 + 三条横切设计线 + 测试组织。想读源码先看这篇。 |
| [接口文档.md](接口文档.md) | **前后端统一开发规范**。按业务模块分组，每个接口含请求路径/方法/参数/响应参数/JSON 样例，另附错误码总表与已知问题。第七节是 Agent 的 5 个接口。 |
| [openapi.exported.json](openapi.exported.json) | springdoc 从代码导出的 OpenAPI（机器生成，勿手改），由 `OpenApiExportIT` 在测试时刷新。 |

## Agent（SP2 · Python）

| 文档 | 说明 |
|---|---|
| [../services/cadence-agent/README.md](../services/cadence-agent/README.md) | **Agent 总览**：快速开始、5 个端点、11 个工具、9 类失败矩阵、实测指标（含未达标项）、与 PRD 的 7 条偏差。 |
| [../services/cadence-agent/docs/loop.md](../services/cadence-agent/docs/loop.md) | **循环状态机与守卫**：每个状态的守卫、三档预算、两层确认令牌、指纹去重、注入防御，以及"删掉某块会怎样"。面试时的答案页。 |
| [../services/cadence-agent/TRACE.md](../services/cadence-agent/TRACE.md) | **一条真实 trace 的逐步解读**（真 DeepSeek 跑的，含一次多余步骤和成本拆解）。 |

> 机器可读的**手写真相**在 [`specs/contracts/openapi.yaml`](../specs/contracts/openapi.yaml)，
> 由 `ContractConformanceIT` 保证与运行时代码一致。

## 改接口的流程

```text
接口文档.md  →  specs/contracts/openapi.yaml  →  代码 + 测试  →  mvn test 全绿
                                                  ↑
                              Agent 侧：pytest tests/ 全绿 + 契约测试
```

1. 先改 `接口文档.md`（前后端一起评审字段名、路径、错误码）
2. 同步 `specs/contracts/openapi.yaml`（机器可读契约）
3. 改后端代码，补/改 `TaskControllerIT`、`ApiResponseIT`、`Sp1CompletionIT`
4. 若涉及 Agent 依赖的字段，同步 `services/cadence-agent/` 并在 `README.md §9` 记录偏差
5. `mvn test` 与 `.\smoke.ps1` 全绿之后才算完成

## 响应外壳速记

```json
{ "code": 0, "message": "success", "data": { ... } }
{ "code": 409, "message": "版本不匹配..." }
```

两个例外：`/stream/{id}` 是 SSE（`id`/`event`/`data` 三行），`/health` 是裸对象。
