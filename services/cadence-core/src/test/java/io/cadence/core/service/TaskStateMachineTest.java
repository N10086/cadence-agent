package io.cadence.core.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import io.cadence.core.AbstractIntegrationTest;
import io.cadence.core.domain.Priority;
import io.cadence.core.domain.TaskStatus;
import io.cadence.core.web.dto.TaskCreateRequest;
import io.cadence.core.web.dto.TaskPatchRequest;
import io.cadence.core.web.error.IllegalTransitionException;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Nested;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;

/**
 * FR-2：状态机。
 *
 * 覆盖 PRD 承诺的全部流转规则，重点是"明确禁止"的那几条
 * —— 那才是状态机存在的理由。
 */
class TaskStateMachineTest extends AbstractIntegrationTest {

    @Autowired TaskService tasks;
    @Autowired JdbcTemplate jdbc;

    @BeforeEach
    void clean() {
        jdbc.execute("TRUNCATE audit_log, idempotency_record, task_dependency, task CASCADE");
    }

    private String newTask() {
        return tasks.create(new TaskCreateRequest(
            "测试任务", null, Priority.NORMAL, null, null, null, null, false, null, List.of())).getId();
    }

    private TaskPatchRequest titleOnly(long ver, String t) {
        return new TaskPatchRequest(ver, t, null, null, null, null, null, null, null, null);
    }

    @Nested
    @DisplayName("TODO → DOING → DONE 正常路径")
    class HappyPath {

        @Test
        void 完整生命周期() {
            String id = newTask();
            assertThat(tasks.getOrThrow(id).getStatus()).isEqualTo(TaskStatus.TODO);

            assertThat(tasks.start(id, 0L, "agent", null).getStatus()).isEqualTo(TaskStatus.DOING);
            assertThat(tasks.complete(id, 1L, "agent", null).getStatus()).isEqualTo(TaskStatus.DONE);
        }

        @Test
        void 从TODO直接完成也可以() {
            String id = newTask();
            assertThat(tasks.complete(id, 0L, "agent", null).getStatus()).isEqualTo(TaskStatus.DONE);
        }

        @Test
        void 重复start是幂等的() {
            String id = newTask();
            tasks.start(id, 0L, "agent", null);
            // 已 DOING 再 start，直接返回，不校验 version
            assertThat(tasks.start(id, 0L, "agent", null).getStatus()).isEqualTo(TaskStatus.DOING);
        }
    }

    @Nested
    @DisplayName("禁止的流转")
    class Forbidden {

        @Test
        @DisplayName("DONE → start 被拒绝（必须先 reopen）")
        void 不可从DONE再start() {
            String id = newTask();
            tasks.complete(id, 0L, "agent", null);

            assertThatThrownBy(() -> tasks.start(id, 1L, "agent", null))
                .isInstanceOf(IllegalTransitionException.class)
                .hasMessageContaining("只有 TODO 状态可以开始");
        }

        @Test
        @DisplayName("CANCELLED → complete 被拒绝")
        void 已取消不可完成() {
            String id = newTask();
            tasks.cancel(id, 0L, "agent", null);

            assertThatThrownBy(() -> tasks.complete(id, 1L, "agent", null))
                .isInstanceOf(IllegalTransitionException.class)
                .hasMessageContaining("已取消");
        }

        @Test
        @DisplayName("DONE → cancel 被拒绝（要先 reopen）")
        void 已完成不可取消() {
            String id = newTask();
            tasks.complete(id, 0L, "agent", null);

            assertThatThrownBy(() -> tasks.cancel(id, 1L, "agent", null))
                .isInstanceOf(IllegalTransitionException.class)
                .hasMessageContaining("已完成的任务不能取消");
        }
    }

    @Nested
    @DisplayName("Reopen 恢复")
    class Reopen {

        @Test
        void 从DONE恢复为TODO() {
            String id = newTask();
            tasks.complete(id, 0L, "agent", null);
            assertThat(tasks.reopen(id, 1L, "agent", null).getStatus()).isEqualTo(TaskStatus.TODO);
            // 恢复后可以重新开始
            assertThat(tasks.start(id, 2L, "agent", null).getStatus()).isEqualTo(TaskStatus.DOING);
        }

        @Test
        void 从CANCELLED恢复为TODO() {
            String id = newTask();
            tasks.cancel(id, 0L, "agent", null);
            assertThat(tasks.reopen(id, 1L, "agent", null).getStatus()).isEqualTo(TaskStatus.TODO);
            // 恢复后可以完成
            assertThat(tasks.complete(id, 2L, "agent", null).getStatus()).isEqualTo(TaskStatus.DONE);
        }

        @Test
        void 从TODO恢复是幂等的() {
            String id = newTask();
            assertThat(tasks.reopen(id, 0L, "agent", null).getStatus()).isEqualTo(TaskStatus.TODO);
        }
    }

    @Nested
    @DisplayName("取消")
    class Cancel {

        @Test
        void 从TODO取消() {
            String id = newTask();
            assertThat(tasks.cancel(id, 0L, "agent", null).getStatus()).isEqualTo(TaskStatus.CANCELLED);
        }

        @Test
        void 从DOING取消() {
            String id = newTask();
            tasks.start(id, 0L, "agent", null);
            assertThat(tasks.cancel(id, 1L, "agent", null).getStatus()).isEqualTo(TaskStatus.CANCELLED);
        }
    }

    @Nested
    @DisplayName("状态机与乐观锁的关系")
    class Interaction {

        @Test
        @DisplayName("状态不允许时，优先报状态错误而不是版本错误")
        void 状态检查先于版本检查() {
            String id = newTask();
            tasks.complete(id, 0L, "agent", null);   // version 变 1

            // 用错误的 version，也用了非法的流转 —— 应该报流转错误
            assertThatThrownBy(() -> tasks.start(id, 999L, "agent", null))
                .isInstanceOf(IllegalTransitionException.class);
        }

        @Test
        @DisplayName("已 DONE 时重复 complete 是幂等的，不校验 version")
        void 完成幂等不校验版本() {
            String id = newTask();
            tasks.complete(id, 0L, "agent", null);
            // 用旧 version 再来一次也不报错
            assertThat(tasks.complete(id, 0L, "agent", null).getStatus()).isEqualTo(TaskStatus.DONE);
        }
    }
}
