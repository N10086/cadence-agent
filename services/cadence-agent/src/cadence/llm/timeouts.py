"""超时矩阵的**唯一**来源（L2-3）。

为什么单独一个模块：

- `config.Settings` 的默认值引用这里的常量，LLM 客户端的兜底默认值也引用它。
  两处都写 `15.0` 会漂移 —— 而超时漂移是那种"只在故障时才现形"的 bug。
- 客户端可以被直接实例化（单测、将来的多 provider），所以它不能把默认值写死；
  但又不能要求调用方一定传值，否则一行 `DeepSeekClient(api_key=...)` 就会炸。

注意：**循环最外层仍会再套一次读超时**（`runtime/loop.py`），
因为"注入的 client 忘了配超时"是真实存在的一类错误。
"""

from __future__ import annotations

#: LLM：建立连接 3s（连不上就赶紧换策略），首字节 15s（长 prompt 的推理确实慢）
DEFAULT_LLM_CONNECT_TIMEOUT_S = 3.0
DEFAULT_LLM_READ_TIMEOUT_S = 15.0

#: Core：本机服务，连接 2s / 读 3s。Core 慢了就该降级，不能占着用户的一轮时间。
DEFAULT_CORE_CONNECT_TIMEOUT_S = 2.0
DEFAULT_CORE_READ_TIMEOUT_S = 3.0

#: 单轮总超时（L3 的 watchdog 会在这里报警并打印协程栈）
DEFAULT_TURN_TIMEOUT_S = 60.0
