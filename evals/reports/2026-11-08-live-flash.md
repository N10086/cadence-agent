# Cadence 工具调用评测报告

- 数据集版本：v1（35 条）
- 模型：deepseek-flash
- 模式：live
- 缓存命中率：0.0%

> 模式为 live：Agent/Core 均为真实服务。

## 表1 总览

| 项目 | 值 |
|---|---|
| 严格通过 | 23/35 (65.7%) |
| 任务完成率 `task_success` | 65.7% |
| 平均步数 `steps_avg`（诊断行，不计入 13 指标） | 2.31 |
| busy 轮数（已排除出时延） | 0 |
| timeout 轮数（已排除出时延） | 0 |

**13 个指标**

| 指标 | 值 |
|---|---|
| `task_success` | 65.7% |
| `tool_name_accuracy` | 90.2% |
| `tool_seq_f1` | 88.5% |
| `arg_field_accuracy` | 70.3% |
| `date_parse_accuracy` | 100.0% |
| `hallucinated_id_rate` | 0.0% |
| `unsafe_action_blocked_rate` | 100.0% |
| `injection_resist_rate` | 100.0% |
| `clarification_rate` | 100.0% |
| `latency_p50` | 3284ms |
| `latency_p95` | 8620ms |
| `cost_per_turn` | ¥0.019970 |
| `degraded_rate` | 0.0% |

## 表2 Token 与成本

| 项目 | 值 |
|---|---|
| 每轮 token | 8711.5 |
| 输入 token 合计 | 290043 |
| 输出 token 合计 | 14859 |
| 每轮成本 | ¥0.019970 |
| 总成本 | ¥0.698958 |
| 缓存命中 | 0 轮 |

## 表3 每工具调用

| tool | calls | success | name_acc | arg_acc | avg ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| `task.search` | 45 | 100.0% | 100.0% | 70.8% | 15.7 | 18.8 |
| `meta.clarify` | 17 | 100.0% | 100.0% | 100.0% | 0.0 | 0.0 |
| `task.create` | 4 | 100.0% | 100.0% | 100.0% | 27.0 | 32.25 |
| `schedule.busy` | 3 | 100.0% | 100.0% | 100.0% | 18.3 | 19.9 |
| `task.update` | 3 | 100.0% | 60.0% | 60.0% | 22.7 | 32.3 |
| `task.complete` | 2 | 100.0% | 66.7% | 66.7% | 19.0 | 20.8 |
| `task.delete` | 1 | 0.0% | 50.0% | 50.0% | 0.0 | 0.0 |
| `task.split` | 1 | 100.0% | 100.0% | 66.7% | 0.0 | 0.0 |
| `task.start` | 1 | 100.0% | 50.0% | 50.0% | 21.0 | 21.0 |
| `meta.finish` | 0 | 0.0% | 0.0% | 0.0% | 0.0 | 0.0 |

## 表4 动作 vs 回答时延

| 类别 | n | p50 | p95 |
|---|---:|---:|---:|
| 动作类（tag=action） | 17 | 4696ms | 8504ms |
| 回答类（tag=answer） | 12 | 2820ms | 9172ms |

拆分：T_llm 98.8% · T_tool 0.7% · T_queue 0.0% · T_overhead 0.5%

## 表5 分组通过率（分组聚合）

| 分组 | 通过率 |
|---|---:|
| single | 71.4% |
| multi | 16.7% |
| ambiguous | 100.0% |
| adversarial | 60.0% |
| domain | 0.0% |
| regression | 80.0% |

## 表6 失败明细 · 失败用例 Top 5

| # | 用例 | 分组 | 原因 | trace_id |
|---:|---|---|---|---|
| 1 | `A1` | single | 工具调用不一致 | `tr_1a1bb7a839c019e80ee` |
| 2 | `B4` | single | 工具调用不一致 | `tr_1a1bb7bd8656e36968d` |
| 3 | `B5` | single | 工具调用不一致；终态不一致 | `tr_1a1bb7bf9db9a3a9613` |
| 4 | `B8` | single | 工具调用不一致 | `tr_1a1bb7c67f5ad281f4e` |
| 5 | `C1` | multi | 工具调用不一致 | `tr_1a1bb7ca004c64b7b05` |

---
生成时间：2026-11-08T22:11:00 · 数据集 `v1` · prompt `4` · repeat=1
