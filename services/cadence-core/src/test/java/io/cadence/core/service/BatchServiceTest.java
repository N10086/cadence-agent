package io.cadence.core.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import io.cadence.core.AbstractIntegrationTest;
import io.cadence.core.domain.Priority;
import io.cadence.core.web.dto.TaskCreateRequest;
import io.cadence.core.web.error.BatchConflictException;
import java.time.OffsetDateTime;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;

class BatchServiceTest extends AbstractIntegrationTest {

    @Autowired BatchService batch;
    @Autowired TaskService tasks;
    @Autowired JdbcTemplate jdbc;

    @BeforeEach
    void clean() {
        jdbc.execute("TRUNCATE task_dependency, task CASCADE");
    }

    private String task(String title) {
        return tasks.create(new TaskCreateRequest(
            title, null, Priority.NORMAL, null, null, null, null, false, null, List.of())).getId();
    }

    private io.cadence.core.web.dto.BatchUpdateRequest.BatchOp move(String id, long version, String iso) {
        return new io.cadence.core.web.dto.BatchUpdateRequest.BatchOp(
            id, version,
            new io.cadence.core.web.dto.BatchUpdateRequest.Patch(
                null, null, null, OffsetDateTime.parse(iso), null, null, null, null, null));
    }

    @Test
    @DisplayName("全部 version 正确 → 全部应用")
    void 全部成功() {
        String a = task("A"), b = task("B"), c = task("C");

        var result = batch.apply(new io.cadence.core.web.dto.BatchUpdateRequest(
            List.of(
                move(a, 0, "2026-11-10T09:00:00+08:00"),
                move(b, 0, "2026-11-11T09:00:00+08:00"),
                move(c, 0, "2026-11-12T09:00:00+08:00"))));

        assertThat(result.applied()).isEqualTo(3);
        assertThat(tasks.getOrThrow(a).getVersion()).isEqualTo(1L);
        assertThat(tasks.getOrThrow(a).getStartAt()).isEqualTo(OffsetDateTime.parse("2026-11-10T09:00:00+08:00"));
    }

    @Test
    @DisplayName("有一条 version 不正确 → 整批回滚，一条都不改")
    void 单条冲突整批回滚() {
        String a = task("A"), b = task("B");

        // b 先被改一次，version 变成 1
        tasks.patch(b, new io.cadence.core.web.dto.TaskPatchRequest(
            0L, "B改过", null, null, null, null, null, null, null, null));

        assertThatThrownBy(() -> batch.apply(new io.cadence.core.web.dto.BatchUpdateRequest(
            List.of(
                move(a, 0, "2026-11-10T09:00:00+08:00"),   // version 正确
                move(b, 0, "2026-11-11T09:00:00+08:00")))))  // version 过期
            .isInstanceOf(BatchConflictException.class);

        // 关键断言：a 也不能被改
        assertThat(tasks.getOrThrow(a).getStartAt()).isNull();
        assertThat(tasks.getOrThrow(a).getVersion()).isEqualTo(0L);
    }

    @Test
    @DisplayName("批次里有不存在的 id → 整批失败")
    void 不存在id整批失败() {
        String a = task("A");
        assertThatThrownBy(() -> batch.apply(new io.cadence.core.web.dto.BatchUpdateRequest(
            List.of(
                move(a, 0, "2026-11-10T09:00:00+08:00"),
                move("00000000-0000-0000-0000-000000000000", 0, "2026-11-11T09:00:00+08:00")))))
            .isInstanceOf(io.cadence.core.service.TaskNotFoundException.class);

        assertThat(tasks.getOrThrow(a).getVersion()).isEqualTo(0L);
    }

    @Test
    @DisplayName("空批次被 Bean Validation 拒（record 层校验在 Controller）；service 层空列表不报错")
    void 空批次() {
        var result = batch.apply(new io.cadence.core.web.dto.BatchUpdateRequest(List.of()));
        assertThat(result.applied()).isZero();
    }

    @Test
    @DisplayName("同一任务在批次里出现两次 → 后一次覆盖前一次")
    void 同任务重复出现() {
        String a = task("A");
        var result = batch.apply(new io.cadence.core.web.dto.BatchUpdateRequest(
            List.of(
                move(a, 0, "2026-11-10T09:00:00+08:00"),
                move(a, 0, "2026-11-20T09:00:00+08:00"))));
        assertThat(result.applied()).isEqualTo(2);
        // 两次都基于同一个实体对象，最终值取最后一次赋值
        assertThat(tasks.getOrThrow(a).getStartAt()).isEqualTo(OffsetDateTime.parse("2026-11-20T09:00:00+08:00"));
    }
}
