-- ============================================================
-- V4 · 演示种子数据（8 条任务）
--
-- 为什么放在迁移里：别人 clone 下来 `docker compose up` 之后，
-- 页面立刻有可操作的任务，不用先跑一堆 curl 造数据。
--
-- 时间基准固定在 2026-11-08 10:30 +08:00，和 evals/datasets/v1 的题面一致；
-- 评测时 runner 会先 reset 再按题面 seed，这些行会被清掉。
--
-- 幂等：ON CONFLICT (id) DO NOTHING —— 重复执行不会产生重复行。
-- id 用固定 UUID，评测和调试时可以直接引用。
-- ============================================================

INSERT INTO task (id, title, description, status, priority, start_at, due_at,
                  estimate_min, parent_id, pinned, rrule, tags, version,
                  deleted_at, created_at, updated_at)
VALUES
-- 牛奶｜买牛奶：TODO / NORMAL / 无截止 / 生活
('bmk00000-0000-4000-8000-000000000001',
 '买牛奶', NULL, 'TODO', 'NORMAL', NULL, NULL, NULL, NULL, FALSE, NULL,
 ARRAY['生活']::TEXT[], 0, NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 周报｜写周报：TODO / HIGH / 11-13 18:00 / work
('bmk00000-0000-4000-8000-000000000002',
 '写周报', NULL, 'TODO', 'HIGH', NULL,
 '2026-11-13T18:00:00+08:00', NULL, NULL, FALSE, NULL,
 ARRAY['work']::TEXT[], 0, NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 演示｜客户演示：TODO / URGENT / 11-11 14:00 开始与截止 / 60 分钟 / work
('bmk00000-0000-4000-8000-000000000003',
 '客户演示', NULL, 'TODO', 'URGENT',
 '2026-11-11T14:00:00+08:00', '2026-11-11T14:00:00+08:00',
 60, NULL, FALSE, NULL,
 ARRAY['work']::TEXT[], 0, NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 论文｜读论文：TODO / NORMAL / 无截止 / 学习
('bmk00000-0000-4000-8000-000000000004',
 '读论文', NULL, 'TODO', 'NORMAL', NULL, NULL, NULL, NULL, FALSE, NULL,
 ARRAY['学习']::TEXT[], 0, NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 房租｜交房租：TODO / NORMAL / 11-30 23:59 / 生活
('bmk00000-0000-4000-8000-000000000005',
 '交房租', NULL, 'TODO', 'NORMAL', NULL,
 '2026-11-30T23:59:00+08:00', NULL, NULL, FALSE, NULL,
 ARRAY['生活']::TEXT[], 0, NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 健身｜健身：TODO / LOW / 每周一三五重复 / 健康
('bmk00000-0000-4000-8000-000000000006',
 '健身', NULL, 'TODO', 'LOW', NULL, NULL, 45, NULL, FALSE,
 'FREQ=WEEKLY;BYDAY=MO,WE,FR;COUNT=10',
 ARRAY['健康']::TEXT[], 0, NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 修 bug｜修 bug #42：TODO / HIGH / 11-12 18:00 / work
('bmk00000-0000-4000-8000-000000000007',
 '修 bug #42', NULL, 'TODO', 'HIGH', NULL,
 '2026-11-12T18:00:00+08:00', 90, NULL, FALSE, NULL,
 ARRAY['work', 'bug']::TEXT[], 0, NULL,
 '2026-11-08T10:30:00+08:00', '2026-11-08T10:30:00+08:00'),

-- 旧任务｜旧任务：CANCELLED / LOW / 无截止 —— 专门给"删除""取消"类用例用
('bmk00000-0000-4000-8000-000000000008',
 '旧任务', '三年前的了，一直没动', 'CANCELLED', 'LOW', NULL, NULL, NULL, NULL, FALSE, NULL,
 ARRAY['杂项']::TEXT[], 0, NULL,
 '2024-01-01T09:00:00+08:00', '2026-11-08T10:30:00+08:00')
ON CONFLICT (id) DO NOTHING;
