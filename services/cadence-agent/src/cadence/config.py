"""配置。全部走环境变量 / .env，代码里没有魔法值。

.env 的位置：
  仓库根      <repo>/.env          ← 放密钥（.gitignore 已忽略）
  服务目录    services/cadence-agent/.env  ← 放本机临时覆盖

优先级：真实环境变量 > 服务目录 .env > 仓库根 .env > 这里的默认值。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .llm.timeouts import (
    DEFAULT_CORE_CONNECT_TIMEOUT_S,
    DEFAULT_CORE_READ_TIMEOUT_S,
    DEFAULT_LLM_CONNECT_TIMEOUT_S,
    DEFAULT_LLM_READ_TIMEOUT_S,
    DEFAULT_TURN_TIMEOUT_S,
)

# src/cadence/config.py -> [0]=cadence [1]=src [2]=cadence-agent [3]=services [4]=<repo>
_SERVICE_DIR = Path(__file__).resolve().parents[2]
_REPO_DIR = Path(__file__).resolve().parents[4]
#: prompts 就在包目录下：src/cadence/prompts（不是 src/prompts）
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_DIR / ".env"), str(_SERVICE_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 服务 ----------
    agent_host: str = "127.0.0.1"
    agent_port: int = Field(18000, description="避开 ecom-gateway 的 8000")
    log_level: str = "INFO"

    # ---------- 下游 Core ----------
    # L2-3 超时矩阵：connect 2s / read 3s。Core 是本机服务，3s 还没回就是出了问题，
    # 不该在这里占着一轮 60s 的用户时间。
    core_base_url: str = "http://localhost:18080"
    core_timeout_s: float = DEFAULT_CORE_READ_TIMEOUT_S
    core_connect_timeout_s: float = DEFAULT_CORE_CONNECT_TIMEOUT_S
    core_retry_times: int = 2
    core_retry_backoff_s: float = 0.5  # 0.5 / 1 / 2

    # ---------- 模型 ----------
    # auto: 有 DEEPSEEK_API_KEY 就走 live，否则 fake
    llm_mode: Literal["auto", "fake", "live"] = "auto"

    # L2-3 超时矩阵：connect 3s / read 15s。
    # read 比 connect 宽得多：连接失败要快速失败，但长 prompt 的推理确实可能要十几秒。
    llm_connect_timeout_s: float = DEFAULT_LLM_CONNECT_TIMEOUT_S
    llm_read_timeout_s: float = DEFAULT_LLM_READ_TIMEOUT_S
    #: 单轮总超时。它必须大于 LLM read 超时，否则还没到 read 超时就被整轮打断，
    #: 降级链（L1→L2→L3）永远跑不完。
    turn_timeout_s: float = DEFAULT_TURN_TIMEOUT_S

    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_model: str = "glm-4-flash"
    #: 单个 provider 的覆盖值；留空则用矩阵默认（connect=llm_connect_timeout_s / read=llm_read_timeout_s）
    zhipu_timeout_s: float = 0.0

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_s: float = 0.0

    # 路由：只有一个 provider 时，仍然决定"用短提示还是长提示"，
    # 让 tier 出现在 trace 里供 SP4 统计；但不会产生跨 provider 的成本差异。
    router_llm_verdict: bool = False
    router_verdict_model: str = "deepseek-chat"

    # 定价（元 / 百万 token）。**必须按官方最新定价核对**，否则成本指标不准。
    deepseek_price_in_per_m: float = 2.0
    deepseek_price_out_per_m: float = 8.0
    zhipu_price_per_m: float = 0.0  # GLM-4-Flash 免费

    # ---------- 循环预算 ----------
    step_budget: int = Field(8, ge=1)
    #: **累计计费** token 上限（不是单次上下文大小，那个看 max_context_tokens）。
    #: 注意：每步都会重发上下文，所以 3 步的实际计费 token 约为单步的 3 倍。
    #: PRD 的 "2200 token/轮" 指的是**单次请求的上下文预算**，对应 max_context_tokens。
    token_budget_per_turn: int = Field(20000, ge=100)
    max_context_tokens: int = Field(4000, ge=500)
    llm_retry_times: int = 1
    llm_retry_backoff_s: float = 0.8

    # ---------- 护栏 ----------
    confirm_ttl_s: int = 300
    destructive_batch_threshold: int = 3
    tool_result_max_items: int = 20
    tool_result_max_chars: int = 1500

    # ---------- 流式 ----------
    # False = 一个 LLM 回合确认成功后再一次性推 token（重试不会重复文本，默认）
    # True  = 边生成边推（体验好，但重试时可能重复一段文本）
    sse_live_tokens: bool = False

    # ---------- 会话 ----------
    session_ttl_s: int = 1800
    session_max_lru: int = 200
    session_recent_turns: int = 3
    session_summary_chars: int = 300
    #: L3-3：同一会话的上一轮没结束时，第二条消息等锁的上限。超时 → 409 SESSION_BUSY。
    session_lock_timeout_s: float = 20.0

    # ---------- 并发（L3） ----------
    #: L3-1 入口准入：同时"排队 + 运行"的 turn 上限。满 → 503 OVERLOADED，不无限排队。
    turn_queue_capacity: int = Field(64, ge=1)
    #: L3-2 全局 LLM 信号量。只在真正调 provider 的那一小段持有（叶子节点）。
    llm_max_concurrency: int = Field(16, ge=1)

    # ---------- 落盘 ----------
    data_dir: str = ""  # 空 = <repo>/data/agent
    trace_dir: str = ""  # 空 = <data_dir>/traces
    session_dir: str = ""  # 空 = <data_dir>/sessions

    # ---------- 熔断 ----------
    breaker_failure_threshold: float = 0.5
    breaker_window: int = 20
    breaker_cooldown_s: float = 30.0
    breaker_cooldown_max_s: float = 300.0

    # ---------- 测试/演示 ----------
    fake_script_path: str = ""  # 空 = 用内置剧本
    #: L4：评测冻结时钟（ISO8601，带时区）。空 = 用真实时间。
    #: 外部 live Agent 需要以同名环境变量启动，才能复现"今天/本周三/本周五"。
    eval_frozen_now: str = ""

    # ------------------------------------------------------------------

    @property
    def resolved_llm_mode(self) -> Literal["fake", "live"]:
        if self.llm_mode == "auto":
            return "live" if self.deepseek_api_key.strip() else "fake"
        return self.llm_mode  # type: ignore[return-value]

    def llm_timeouts(self, provider: str = "deepseek") -> tuple[float, float]:
        """返回 (connect, read)。provider 覆盖值为 0 时回落到矩阵默认。

        超时只允许在这两个地方定义：矩阵默认与单项覆盖。禁止在客户端或 loop 里写死数字，
        否则「确认版本那次 GET 用了 LLM 的 15s」这类串台会再次发生。
        """
        override = self.zhipu_timeout_s if provider == "zhipu" else self.deepseek_timeout_s
        read = override if override and override > 0 else self.llm_read_timeout_s
        return self.llm_connect_timeout_s, read

    @property
    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir) if self.data_dir else _REPO_DIR / "data" / "agent"

    @property
    def resolved_trace_dir(self) -> Path:
        return Path(self.trace_dir) if self.trace_dir else self.resolved_data_dir / "traces"

    @property
    def resolved_session_dir(self) -> Path:
        return Path(self.session_dir) if self.session_dir else self.resolved_data_dir / "sessions"

    def ensure_dirs(self) -> None:
        for d in (self.resolved_data_dir, self.resolved_trace_dir, self.resolved_session_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：改环境变量后重新读。"""
    get_settings.cache_clear()
