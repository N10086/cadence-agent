-- ============================================================
-- V1 · 任务表
-- 设计要点：
--   1. 主键是 UUID 字符串 —— 自增 id 会让模型"猜" id，幻觉率飙升
--   2. 时间是 timestamptz —— 保留"下周三"的时区语义
--   3. version 是乐观锁列，由 JPA @Version 维护
--   4. deleted_at 软删除，所有查询索引都带 WHERE deleted_at IS NULL
-- ============================================================

CREATE TABLE task (
    id            VARCHAR(36)   PRIMARY KEY,
    title         VARCHAR(200)  NOT NULL,
    description   TEXT,
    status        VARCHAR(16)   NOT NULL,
    priority      VARCHAR(16)   NOT NULL,
    start_at      TIMESTAMPTZ,
    due_at        TIMESTAMPTZ,
    estimate_min  INTEGER       CHECK (estimate_min IS NULL OR (estimate_min >= 5 AND estimate_min <= 480)),
    parent_id     VARCHAR(36)   REFERENCES task (id),
    pinned        BOOLEAN       NOT NULL DEFAULT FALSE,
    rrule         VARCHAR(200),
    tags          TEXT[]        NOT NULL DEFAULT '{}',
    version       BIGINT        NOT NULL DEFAULT 0,
    deleted_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ   NOT NULL,
    updated_at    TIMESTAMPTZ   NOT NULL
);

-- 主查询：状态 + 截止期。部分索引，避免索引里一半是已删除的行
CREATE INDEX idx_task_status_due
    ON task (status, due_at)
    WHERE deleted_at IS NULL;

-- 查询某个父任务下的子任务
CREATE INDEX idx_task_parent
    ON task (parent_id)
    WHERE deleted_at IS NULL AND parent_id IS NOT NULL;

-- 重复任务展开
CREATE INDEX idx_task_rrule
    ON task (rrule)
    WHERE deleted_at IS NULL AND rrule IS NOT NULL;

-- 标签过滤
CREATE INDEX idx_task_tags
    ON task USING GIN (tags);
