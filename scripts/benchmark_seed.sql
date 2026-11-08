-- ============================================================================
-- Cadence benchmark 库：8 条背景任务数据（修正版）
-- 目标库：benchmark（PostgreSQL 16）
-- 数据来源：evals/datasets/v1 共用 setup（时间基准 2026-11-08 10:30 +08:00）
--
-- ⚠️ 修正记录：上一版多写了一列 version。Core 的 @Version 是 Hibernate
--    运行时列，Flyway 的 V1__init_task.sql 并没有它；引用该列会导致
--    INSERT 全部失败（而 CREATE TABLE 已经成功），表现为"有表没数据"。
--
-- 用法：DBeaver 连 benchmark 库，整段执行。可重复执行（ON CONFLICT DO NOTHING）。
-- ============================================================================

INSERT INTO task (id, title, description, status, priority, start_at, due_at,
                  estimate_min, parent_id, pinned, rrule, tags, deleted_at,
                  created_at, updated_at)
VALUES
-- 牛奶｜买牛奶：TODO / NORMAL / 无截止 / 生活
('bmk00000-0000-4000-8000-000000000001',
 '买牛奶', NULL, 'TODO', 'NORMAL', NULL, NULL, NULL, NULL, FALSE, NULL,
 ARRAY['生活']::TEXT[], NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 周报｜写周报：TODO / HIGH / 11-13 18:00 / work
('bmk00000-0000-4000-8000-000000000002',
 '写周报', NULL, 'TODO', 'HIGH', NULL,
 '2026-11-13T18:00:00+08:00', NULL, NULL, FALSE, NULL,
 ARRAY['work']::TEXT[], NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 演示｜客户演示：TODO / URGENT / 11-11 14:00 开始与截止 / 60 分钟 / work
('bmk00000-0000-4000-8000-000000000003',
 '客户演示', NULL, 'TODO', 'URGENT',
 '2026-11-11T14:00:00+08:00', '2026-11-11T14:00:00+08:00',
 60, NULL, FALSE, NULL,
 ARRAY['work']::TEXT[], NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 论文｜读论文：TODO / NORMAL / 无截止 / 学习
('bmk00000-0000-4000-8000-000000000004',
 '读论文', NULL, 'TODO', 'NORMAL', NULL, NULL, NULL, NULL, FALSE, NULL,
 ARRAY['学习']::TEXT[], NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 房租｜交房租：TODO / NORMAL / 11-30 23:59 / 生活
('bmk00000-0000-4000-8000-000000000005',
 '交房租', NULL, 'TODO', 'NORMAL', NULL,
 '2026-11-30T23:59:00+08:00', NULL, NULL, FALSE, NULL,
 ARRAY['生活']::TEXT[], NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 健身｜健身：TODO / LOW / 无截止 / 健康
('bmk00000-0000-4000-8000-000000000006',
 '健身', NULL, 'TODO', 'LOW', NULL, NULL, NULL, NULL, FALSE, NULL,
 ARRAY['健康']::TEXT[], NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 修bug｜修 bug #42：TODO / HIGH / 11-12 18:00 / work（依赖 论文）
('bmk00000-0000-4000-8000-000000000007',
 '修 bug #42', NULL, 'TODO', 'HIGH', NULL,
 '2026-11-12T18:00:00+08:00', NULL, NULL, FALSE, NULL,
 ARRAY['work']::TEXT[], NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 旧事｜旧任务：CANCELLED / LOW / 无截止 / 无标签
('bmk00000-0000-4000-8000-000000000008',
 '旧任务', NULL, 'CANCELLED', 'LOW', NULL, NULL, NULL, NULL, FALSE, NULL,
 ARRAY[]::TEXT[], NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00')

ON CONFLICT (id) DO NOTHING;

-- 依赖：修 bug #42 依赖 读论文
INSERT INTO task_dependency (task_id, depends_on_id, created_at)
VALUES ('bmk00000-0000-4000-8000-000000000007',
        'bmk00000-0000-4000-8000-000000000004',
        '2026-11-08T10:30:00+08:00')
ON CONFLICT (task_id, depends_on_id) DO NOTHING;

-- 校验：应返回 8 行
SELECT
    t.id,
    t.title,
    t.status,
    t.priority,
    to_char(t.start_at AT TIME ZONE 'Asia/Shanghai', 'MM-DD HH24:MI') AS start_at,
    to_char(t.due_at   AT TIME ZONE 'Asia/Shanghai', 'MM-DD HH24:MI') AS due_at,
    t.estimate_min,
    t.tags
FROM task t
WHERE t.id LIKE 'bmk00000-%'
ORDER BY t.id;
