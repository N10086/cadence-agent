-- ============================================================
-- V2 · 任务依赖
-- 语义：depends_on_id 是前置，task_id 是后继。
--       FINISH_TO_START：前置未完成时，后继不能进入 DONE。
-- 复合主键保证同一对依赖只存在一次。
-- 两个方向的索引都需要：查"我依赖谁"和查"谁依赖我"。
-- ============================================================

CREATE TABLE task_dependency (
    task_id       VARCHAR(36) NOT NULL REFERENCES task (id) ON DELETE CASCADE,
    depends_on_id VARCHAR(36) NOT NULL REFERENCES task (id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (task_id, depends_on_id),

    -- 自己不能依赖自己
    CONSTRAINT chk_no_self_dependency CHECK (task_id <> depends_on_id)
);

CREATE INDEX idx_dep_depends_on ON task_dependency (depends_on_id);
