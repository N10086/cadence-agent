-- ============================================================
-- V3 · 审计日志 + 幂等记录
--
-- 审计表（FR-11）：
--   记录每一次写操作的 actor / trace_id / before / after。
--   这不是"调试日志"——调试信息在应用日志里。这里是业务审计：
--   "谁在什么时候把哪个任务从什么状态改成了什么状态"。
--
-- 幂等表（FR-10）：
--   创建类接口带 X-Idempotency-Key，同一键重复请求返回首次结果。
--   24 小时 TTL，靠定时清理，不然半年后几十万行。
-- ============================================================

CREATE TABLE audit_log (
    id          BIGSERIAL    PRIMARY KEY,
    task_id     VARCHAR(36)  NOT NULL,
    action      VARCHAR(32)  NOT NULL,   -- CREATE / UPDATE / COMPLETE / DELETE / BATCH_UPDATE
    actor       VARCHAR(64)  NOT NULL,   -- 调用方标识，来自 X-Actor，默认 anonymous
    trace_id    VARCHAR(64),             -- 来自 X-Trace-Id，用于和 Agent 侧 trace 关联
    before_json JSONB,
    after_json  JSONB,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_task      ON audit_log (task_id, created_at DESC);
CREATE INDEX idx_audit_trace     ON audit_log (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX idx_audit_created   ON audit_log (created_at DESC);

CREATE TABLE idempotency_record (
    idem_key    VARCHAR(128) PRIMARY KEY,
    task_id     VARCHAR(36)  NOT NULL,
    response    JSONB        NOT NULL,   -- 首次请求的完整响应体，重复请求原样返回
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_idem_created ON idempotency_record (created_at);
