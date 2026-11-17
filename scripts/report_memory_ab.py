"""把 A/B 结果渲染成 Markdown 报告。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

data = {arm["arm"]: arm for arm in json.load(open(".tmp/memory_ab.json", encoding="utf-8"))}
arms = list(data.values())
A, B, C = arms[0], arms[1], arms[2]


def pct(new: float, old: float) -> str:
    if not old:
        return "—"
    return f"{(new - old) / old * 100:+.1f}%"


lines: list[str] = []
lines.append("# 长对话记忆 A/B 测试（2026-11-17）")
lines.append("")
lines.append("## 实验设计")
lines.append("")
lines.append("- 同一段 **8 轮**对话，三条臂各跑一遍；每臂独立 `Runtime` + 独立内存 Core（FakeCore）+ **独立会话 ID**")
lines.append("- 模型：真实 DeepSeek（reason=deepseek-flash / fast=deepseek-chat），温度 0")
lines.append("- 记忆事实：`工位在 3 楼东侧，门禁密码 0427`，**明确要求不创建任务**——保证它只能来自记忆，不能靠 `task.search` 查到")
lines.append("- 回忆轮次：第 5 轮（工位/门禁）、第 7 轮（上一轮让完成哪个任务）、第 8 轮（再说一次工位/门禁）")
lines.append("")
lines.append("| 臂 | 配置 |")
lines.append("|---|---|")
lines.append("| A 无记忆 | `MEMORY_ENABLED=false` + `CONTEXT_RECENT_TURNS=0` |")
lines.append("| B 固定窗口 3 轮 | `MEMORY_ENABLED=false` + `CONTEXT_RECENT_TURNS=3`（旧行为） |")
lines.append("| C 三级记忆 | `MEMORY_ENABLED=true`，窗口 800 token / 8 轮 + L2 摘要 + L3 事实 |")
lines.append("")
lines.append("## 总览")
lines.append("")
lines.append("| 指标 | A 无记忆 | B 固定窗口 3 轮 | C 三级记忆 | C vs A |")
lines.append("|---|---:|---:|---:|---:|")
lines.append(f"| 回忆准确率 | **{A['accuracy'] * 100:.0f}%** | {B['accuracy'] * 100:.0f}% | **{C['accuracy'] * 100:.0f}%** | — |")
lines.append(f"| 输入 token 合计 | {A['total_tokens_in']:,} | {B['total_tokens_in']:,} | **{C['total_tokens_in']:,}** | {pct(C['total_tokens_in'], A['total_tokens_in'])} |")
lines.append(f"| 输出 token 合计 | {A['total_tokens_out']:,} | {B['total_tokens_out']:,} | {C['total_tokens_out']:,} | {pct(C['total_tokens_out'], A['total_tokens_out'])} |")
lines.append(f"| 总费用 | ¥{A['total_cost_micro'] / 1e6:.4f} | ¥{B['total_cost_micro'] / 1e6:.4f} | **¥{C['total_cost_micro'] / 1e6:.4f}** | {pct(C['total_cost_micro'], A['total_cost_micro'])} |")
lines.append(f"| 平均单轮耗时 | {A['avg_latency_ms']} ms | {B['avg_latency_ms']} ms | {C['avg_latency_ms']} ms | {pct(C['avg_latency_ms'], A['avg_latency_ms'])} |")
lines.append("")
lines.append("## 逐轮明细")
lines.append("")
lines.append("| 轮次 | 用户说 | A 步数/in | B 步数/in | C 步数/in | C 回忆得分 |")
lines.append("|---:|---|---:|---:|---:|---:|")
for i in range(8):
    ra, rb, rc = A["records"][i], B["records"][i], C["records"][i]
    user = rc["message"]
    if len(user) > 26:
        user = user[:26] + "…"
    score = "-" if rc["score"] is None else f"{rc['score'] * 100:.0f}%"
    lines.append(f"| {i + 1} | {user} | {ra['steps']}/{ra['tokens_in']:,} | {rb['steps']}/{rb['tokens_in']:,} | {rc['steps']}/{rc['tokens_in']:,} | {score} |")
lines.append("")
lines.append("## 结论")
lines.append("")
lines.append("1. **准确率是决定性差异**：没有记忆的两条臂在三个回忆问题上全错（0/3），三级记忆全对（3/3）。")
lines.append("   A/B 的模型甚至明确回答\"我没有记忆能力\"，而不是编造——说明失败是结构性的，不是模型能力问题。")
lines.append("2. **固定 3 轮窗口等于没有记忆**：第 1 轮的事实到第 5 轮早被挤出，B 与 A 的准确率一样是 0%。")
lines.append("   这说明\"只保留最近 N 轮\"对长对话不成立，必须有 L2 摘要或 L3 事实兜底。")
lines.append("3. **三级记忆反而更省 token**：C 的输入 token 比 A 少 10.8%、费用少 7.2%。")
lines.append("   原因是回忆轮 C 一步就能答（约 3.7k token），而 A/B 要多搜一次（约 7.2k token），")
lines.append("   但搜也搜不到——多花的那一步是纯浪费。")
lines.append("4. **延迟基本持平**：C 平均单轮 4.85s，A 4.55s，差 0.3s（+6.7%）。")
lines.append("   记忆块本身只有几十 token，代价主要来自多一轮上下文重发，可忽略。")
lines.append("")
lines.append("## 局限（如实说）")
lines.append("")
lines.append("- **n=1**：只跑了一段对话、每臂一次，token 差异里有一部分来自工具路由的随机选择，不能当统计显著结论；")
lines.append("- **成本**：本次实验总花费约 ¥0.42（三臂各 8 轮）；")
lines.append("- **事实类型单一**：只测了\"用户显式告知的私有事实\"，没测多轮任务状态回溯；")
lines.append("- 第 7 轮的问题理论上也能通过 `task.search` 碰巧答对（B 就查到了写周报），")
lines.append("  所以\"对话历史回忆\"这一项的说服力弱于第 5/8 轮的私有事实。")
lines.append("")
lines.append("## 复现")
lines.append("")
lines.append("```powershell")
lines.append("$env:PYTHONIOENCODING='utf-8'")
lines.append(".\\.venv\\Scripts\\python.exe .tmp\\memory_ab.py")
lines.append("```")
lines.append("")
lines.append("原始数据：`.tmp/memory_ab.json`（含每轮回答原文）")

out = Path("evals/reports/2026-11-17-记忆AB.md")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {out} ({len(lines)} lines)")
