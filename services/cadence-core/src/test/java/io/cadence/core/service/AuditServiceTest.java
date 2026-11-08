package io.cadence.core.service;

import static org.assertj.core.api.Assertions.assertThat;

import io.cadence.core.AbstractIntegrationTest;
import io.cadence.core.domain.Priority;
import io.cadence.core.web.dto.TaskCreateRequest;
import io.cadence.core.web.dto.TaskPatchRequest;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;

/**
 * FR-11：审计日志。
 *
 * 验证的核心不是"有没有记录"，而是：
 *   1. 每次写操作都留痕
 *   2. before/after 真的反映状态变化
 *   3. trace_id 能把一次对话的多步改动串起来
 */
class AuditServiceTest extends AbstractIntegrationTest {

    @Autowired TaskService tasks;
    @Autowired AuditService audit;
    @Autowired IdempotencyService idem;
    @Autowired JdbcTemplate jdbc;

    @BeforeEach
    void clean() {
        jdbc.execute("TRUNCATE audit_log, idempotency_record, task_dependency, task CASCADE");
    }

    private String task(String actor, String trace) {
        return tasks.create(new TaskCreateRequest(
            "任务", null, Priority.NORMAL, null, null, null, null, false, null, List.of()),
            actor, trace).getId();
    }

    @Test
    @DisplayName("创建会留下一条 CREATE 记录")
    void 创建留痕() {
        String id = task("agent", "tr_001");

        var history = audit.history(id);
        assertThat(history).hasSize(1);
        assertThat(history.get(0).getAction()).isEqualTo("CREATE");
        assertThat(history.get(0).getActor()).isEqualTo("agent");
        assertThat(history.get(0).getTraceId()).isEqualTo("tr_001");
        assertThat(history.get(0).getBeforeJson()).isNull();
        assertThat(history.get(0).getAfterJson()).contains("任务");
    }

    @Test
    @DisplayName("更新记录 before/after 快照")
    void 更新留痕() {
        String id = task("agent", "tr_001");
        tasks.patch(id, new TaskPatchRequest(0L, "改后的标题", null, null, null, null, null, null, null, null),
                    "agent", "tr_001");

        var history = audit.history(id);   // 倒序
        assertThat(history).hasSize(2);
        assertThat(history.get(0).getAction()).isEqualTo("UPDATE");
        assertThat(history.get(0).getBeforeJson()).contains("任务");
        assertThat(history.get(0).getAfterJson()).contains("改后的标题");
    }

    @Test
    @DisplayName("状态流转各记一条")
    void 状态流转留痕() {
        String id = task("agent", "tr_002");
        tasks.start(id, 0L, "agent", "tr_002");
        tasks.complete(id, 1L, "agent", "tr_002");

        var actions = audit.history(id).stream().map(a -> a.getAction()).toList();
        assertThat(actions).containsExactly("COMPLETE", "START", "CREATE");
    }

    @Test
    @DisplayName("按 trace_id 能把一次对话的多步操作串起来")
    void 按trace串联() {
        String a = task("agent", "tr_abc");
        String b = task("agent", "tr_abc");
        tasks.complete(a, 0L, "agent", "tr_abc");

        var byTrace = audit.byTrace("tr_abc");
        assertThat(byTrace).hasSize(3);
        assertThat(byTrace.stream().map(x -> x.getTaskId()).distinct()).containsExactlyInAnyOrder(a, b);
    }

    @Test
    @DisplayName("删除记录 after 为空")
    void 删除留痕() {
        String id = task("agent", "tr_001");
        tasks.softDelete(id, "agent", "tr_001");

        var latest = audit.history(id).get(0);
        assertThat(latest.getAction()).isEqualTo("DELETE");
        assertThat(latest.getBeforeJson()).isNotNull();
        assertThat(latest.getAfterJson()).isNull();
    }

    @Test
    @DisplayName("actor 缺省为 anonymous")
    void 缺省actor() {
        String id = tasks.create(new TaskCreateRequest(
            "匿名任务", null, Priority.NORMAL, null, null, null, null, false, null, List.of())).getId();

        assertThat(audit.history(id).get(0).getActor()).isEqualTo("anonymous");
    }

    @Test
    @DisplayName("业务回滚时审计也回滚（不产生假账）")
    void 回滚不留假账() {
        // 一次失败的更新：version 错误 → 抛异常 → 事务回滚
        String id = task("agent", "tr_x");
        int beforeCount = audit.history(id).size();

        try {
            tasks.patch(id, new TaskPatchRequest(999L, "不该生效", null, null, null, null, null, null, null, null),
                        "agent", "tr_x");
        } catch (Exception ignored) {
        }

        assertThat(audit.history(id)).hasSize(beforeCount);
    }
}
