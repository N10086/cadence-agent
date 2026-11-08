package io.cadence.core.service;

import static org.assertj.core.api.Assertions.assertThat;

import io.cadence.core.AbstractIntegrationTest;
import io.cadence.core.domain.Priority;
import io.cadence.core.web.dto.TaskCreateRequest;
import java.time.OffsetDateTime;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;

class ConflictServiceTest extends AbstractIntegrationTest {

    @Autowired ConflictService conflicts;
    @Autowired TaskService tasks;
    @Autowired JdbcTemplate jdbc;

    @BeforeEach
    void clean() {
        jdbc.execute("TRUNCATE task_dependency, task CASCADE");
    }

    private String taskAt(String title, String startIso, String dueIso, Integer minutes) {
        return tasks.create(new TaskCreateRequest(
            title, null, Priority.NORMAL,
            startIso == null ? null : OffsetDateTime.parse(startIso),
            dueIso == null ? null : OffsetDateTime.parse(dueIso),
            minutes, null, false, null, List.of())).getId();
    }

    @Test
    @DisplayName("两个任务时段重叠 → 检测到冲突")
    void 时段重叠() {
        String a = taskAt("写周报", "2026-11-05T09:00:00+08:00", null, 120);   // 09:00-11:00
        String b = taskAt("看论文", "2026-11-05T10:00:00+08:00", null, 60);    // 10:00-11:00

        var found = conflicts.detect(a, OffsetDateTime.parse("2026-11-05T09:00:00+08:00"), null, 120);
        assertThat(found).hasSize(1);
        assertThat(found.get(0).taskId()).isEqualTo(b);
        assertThat(found.get(0).overlapMinutes()).isEqualTo(60);
    }

    @Test
    @DisplayName("时段相接不算重叠（半开区间）")
    void 相接不算冲突() {
        String a = taskAt("任务A", "2026-11-05T09:00:00+08:00", null, 60);    // 09:00-10:00
        taskAt("任务B", "2026-11-05T10:00:00+08:00", null, 60);               // 10:00-11:00

        var found = conflicts.detect(a, OffsetDateTime.parse("2026-11-05T09:00:00+08:00"), null, 60);
        assertThat(found).isEmpty();
    }

    @Test
    @DisplayName("已完成的任务不参与冲突检测")
    void 完成任务不算冲突() {
        String done = taskAt("已完成", "2026-11-05T09:00:00+08:00", null, 120);
        tasks.complete(done, 0L);

        var found = conflicts.detect(null, OffsetDateTime.parse("2026-11-05T09:00:00+08:00"), null, 60);
        assertThat(found).isEmpty();
    }

    @Test
    @DisplayName("没有工时估算的任务不参与冲突检测")
    void 无工时不算冲突() {
        taskAt("无工时", "2026-11-05T09:00:00+08:00", null, null);
        var found = conflicts.detect(null, OffsetDateTime.parse("2026-11-05T09:00:00+08:00"), null, 60);
        assertThat(found).isEmpty();
    }

    @Test
    @DisplayName("用 dueAt - estimate 反推占用时段")
    void 只有截止时间也能算占用() {
        // 11:00 截止，需要 60 分钟 → 占用 10:00-11:00
        taskAt("截止型", null, "2026-11-05T11:00:00+08:00", 60);

        var found = conflicts.detect(null, OffsetDateTime.parse("2026-11-05T10:30:00+08:00"), null, 60);
        assertThat(found).hasSize(1);
        assertThat(found.get(0).overlapMinutes()).isEqualTo(30);
    }

    @Test
    @DisplayName("忙闲查询返回合并后的区间")
    void 忙闲合并相邻区间() {
        taskAt("A", "2026-11-05T09:00:00+08:00", null, 60);   // 09:00-10:00
        taskAt("B", "2026-11-05T10:00:00+08:00", null, 60);   // 10:00-11:00  → 与 A 相接
        taskAt("C", "2026-11-05T14:00:00+08:00", null, 30);   // 14:00-14:30  → 独立

        var busy = conflicts.busyRanges(
            OffsetDateTime.parse("2026-11-05T00:00:00+08:00"),
            OffsetDateTime.parse("2026-11-06T00:00:00+08:00"));

        assertThat(busy).hasSize(2);
        assertThat(busy.get(0).start()).isEqualTo(OffsetDateTime.parse("2026-11-05T09:00:00+08:00"));
        assertThat(busy.get(0).end()).isEqualTo(OffsetDateTime.parse("2026-11-05T11:00:00+08:00"));
        assertThat(busy.get(1).durationMinutes()).isEqualTo(30);
    }

    @Test
    @DisplayName("忙闲查询按区间裁剪")
    void 忙闲裁剪() {
        taskAt("长任务", "2026-11-05T09:00:00+08:00", null, 480);   // 09:00-17:00

        var busy = conflicts.busyRanges(
            OffsetDateTime.parse("2026-11-05T12:00:00+08:00"),
            OffsetDateTime.parse("2026-11-05T14:00:00+08:00"));

        assertThat(busy).hasSize(1);
        assertThat(busy.get(0).start()).isEqualTo(OffsetDateTime.parse("2026-11-05T12:00:00+08:00"));
        assertThat(busy.get(0).end()).isEqualTo(OffsetDateTime.parse("2026-11-05T14:00:00+08:00"));
    }
}
