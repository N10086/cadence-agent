# 一条真实 trace 的逐步解读

> 这份文件对应 PRD 交付物第 4 条："一份 `TRACE.md`：贴一条真实 trace，逐步讲解"。
> 下面这条是 **2026-11-08 用真 DeepSeek 跑出来的原始记录**，没有修饰，
> 包括那次**多花的一步**。

---

## 1. 怎么看到的

```powershell
.\.venv\Scripts\python.exe -m cadence.cli trace tr_1a1b92561255f00f5a4
```

```
trace tr_1a1b92561255f00f5a4  session=s_trace_doc  steps=8  6460ms  ¥0.0197
------------------------------------------------------------------------------
 1. [ok ] LLM     route.classify             0ms
 2. [ok ] GUARD   guard.classify             0ms
 3. [ok ] LLM     llm.step1               2423ms in=2776 out=103
 4. [ok ] TOOL    task.search              1462ms
        找到 0 条，共 0 条
 5. [ok ] LLM     llm.step2               1197ms in=2964 out=53
 6. [ok ] TOOL    task.search                11ms
        找到 0 条，共 0 条
 7. [ok ] LLM     llm.step3               1356ms in=3102 out=98
 8. [ok ] TOOL    meta.finish                0ms
        给出最终回复
```

用户说的是：**"看看我今天有什么任务"**

---

## 2. 逐行讲

### 第 1 行 · `route.classify`（LLM 类，但没调模型，0ms）

```json
{"tier":"fast","rule":"pure-query","reason":"纯查询"}
```

**规则先拦。** "有什么任务"命中纯查询模式，直接判 `fast`，
省掉一次"让模型判断这句话难不难"的调用。

如果这里判成 `reason`，后面会换更长的 system prompt，
每步多花约 300 token —— 这就是路由在省钱。

### 第 2 行 · `guard.classify`（GUARD，0ms）

```json
{"intent":"query","ambiguous":false,"note":"查询意图"}
```

歧义检测：这句话没有指代词、没有万能动词，`ambiguous=false`，**不需要停下来问**。

如果真的判成有歧义，这里就不会有第 3 行，而是直接推一个 `clarify` 事件然后结束。
**"少走一步"比"多走一步再纠正"便宜得多。**

### 第 3 行 · `llm.step1`（LLM，2423ms，2776 in / 103 out）

```json
{"tier":"fast","model":"deepseek-flash","tool_calls":["task.search"]}
```

第一次调模型。它没有直接回答，而是**要求调 `task.search`**。

注意 `tokens_in=2776` —— 这是每次调用最大的一块成本。拆开看：

| 组成 | 约多少 token |
|---|---|
| 工具 schema（9 个） | ~1400 |
| system prompt（fast 版） | ~200 |
| 当前时间 + 会话 | ~100 |
| 累计的工具结果 | ~1000 |

**这 2776 是每一轮都要重发的。** 所以"少走一轮"省下的不只是时间。

### 第 4 行 · `task.search`（TOOL，1462ms）

```json
{"summary":"找到 0 条，共 0 条"}
```

1462ms 里 99% 是 Core 那边的时间（首次请求要建连接池、Flyway 后的冷查询）。
后面的同类调用只用 11ms —— **第一次和第一百次不是一个量级**，
这也是为什么"启动预热"值得做。

结果是 0 条：今天确实没有截止的任务。**这是数据事实，不是故障。**

### 第 5 行 · `llm.step2`（LLM，1197ms，2964 in / 53 out）

模型拿到"0 条"之后，又要求调一次 `task.search`（换了过滤条件）。

```json
{"tool_calls":["task.search"]}
```

**这一行是一次浪费。** 模型本可以直接回答"今天没有任务"，
但它选择再查一次 —— 大概率是因为第 4 行的数据块里它没读到信心。

这类浪费是**真实存在的**，也是为什么要有步数预算：
单次浪费不要紧，8 步上限保证它不会一直浪费下去。

> 改进方向（留给 SP3）：在工具返回里加一句 `"total":0` 的显式提示，
> 或者让 `meta.finish` 的 schema 描述更强调"信息足够就结束"。

### 第 6 行 · `task.search`（TOOL，11ms）

第二次查询，仍然 0 条。**11ms** —— 连接池已经热了。

对比第 4 行的 1462ms，差了 130 倍。同一个工具、同一份数据。

### 第 7 行 · `llm.step3`（LLM，1356ms，3102 in / 98 out）

```json
{"tool_calls":["meta.finish"]}
```

模型终于决定结束，调用 `meta.finish`。

**为什么非要它调一个"结束工具"**：这样"结束"就变成一个**可统计的显式选择**，
而不是"模型什么时候不说话"。评测时能区分：

- 它是因为信息够了才结束
- 还是因为被打断才结束

### 第 8 行 · `meta.finish`（TOOL，0ms）

`actions_taken` 里只有只读描述（"查询了..."），
`guard` 会放行 —— 读操作不进 `actions_taken`，但 trace 里有记录。

**如果模型声称"我已经把任务排好了"而本轮没有任何写操作，这一行会被标红**
（`guard.actions_unverified`），回复里追加一句免责说明。这条护栏防的是
最伤用户信任的一类错误：**编造已经做完的事**。

---

## 3. 这一轮的钱花在哪

| 步骤 | 计费 token | 说明 |
|---|---|---|
| step1 | 2776 + 103 | 必须的 |
| step2 | 2964 + 53 | **可以省** |
| step3 | 3102 + 98 | 必须的 |
| 合计 | 约 9000 | ¥0.0197 |

**如果把第 5 行省掉，这一轮约 ¥0.013、4.5s。**

这也解释了 README 里的负结果：单轮 ¥0.01 的目标，
在"至少两次 LLM 往返"的前提下本来就很紧，多一步就破。

---

## 4. 从这条 trace 能看出什么设计意图

| 现象 | 背后的设计 |
|---|---|
| 前两行 0ms | 能规则判断的绝不调模型 |
| `tokens_in` 每轮 2800+ | schema 和 system prompt 是固定开销，必须抠 |
| 第一次工具 1462ms、第二次 11ms | 冷启动成本真实存在，p95 被它拉高 |
| 第 5 行多走一步 | 模型会浪费，所以必须有步数预算 |
| `meta.finish` 是工具 | 把"结束"变成可统计的显式选择 |
| `kind` 里有 GUARD | 护栏动作和时间一样重要，必须进同一条时间线 |

---

## 5. 怎么自己复现

```powershell
cd services/cadence-agent
$env:PYTHONPATH='src'
$env:LLM_MODE='live'

# 发一轮
curl -X POST http://localhost:18000/chat -H "Content-Type: application/json" `
  -d '{\"sessionId\":\"s_demo\",\"message\":\"看看我今天有什么任务\"}'

# 拿 traceId 看时间线
.\.venv\Scripts\python.exe -m cadence.cli trace tr_xxxx
.\.venv\Scripts\python.exe -m cadence.cli list      # 列出最近的 trace
```

原始文件在 `data/agent/traces/<日期>/<trace_id>.jsonl`，
**一次对话一个文件**，直接发给别人就能复现。
