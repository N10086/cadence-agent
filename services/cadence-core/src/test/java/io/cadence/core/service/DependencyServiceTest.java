package io.cadence.core.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import io.cadence.core.AbstractIntegrationTest;
import io.cadence.core.web.dto.TaskCreateRequest;
import io.cadence.core.web.error.DependencyCycleException;
import io.cadence.core.web.error.DependencyViolationException;
import io.cadence.core.domain.Priority;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;

class DependencyServiceTest extends AbstractIntegrationTest {

    @Autowired DependencyService deps;
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

    @Test
    @DisplayName("A 依赖 B：B 未完成时 A 不能完成")
    void 前置未完成阻止完成() {
        String b = task("前置");
        String a = task("后继");

        deps.addDependency(a, b);

        assertThat(deps.prerequisitesOf(a)).containsExactly(b);
        assertThat(deps.dependentsOf(b)).containsExactly(a);

        assertThatThrownBy(() -> tasks.complete(a, 0L))
            .isInstanceOf(DependencyViolationException.class)
            .hasMessageContaining("前置任务尚未完成");

        // B 完成后，A 可以完成
        tasks.complete(b, 0L);
        assertThat(tasks.complete(a, 0L).getStatus().name()).isEqualTo("DONE");
    }

    @Test
    @DisplayName("自己依赖自己 → 拒绝")
    void 自依赖被拒绝() {
        String a = task("A");
        assertThatThrownBy(() -> deps.addDependency(a, a))
            .isInstanceOf(DependencyViolationException.class)
            .hasMessageContaining("不能依赖自己");
    }

    @Test
    @DisplayName("A→B→C，再加 C→A 形成环 → 拒绝")
    void 三元环被拒绝() {
        String a = task("A");
        String b = task("B");
        String c = task("C");

        deps.addDependency(a, b);   // A 依赖 B
        deps.addDependency(b, c);   // B 依赖 C

        assertThatThrownBy(() -> deps.addDependency(c, a))
            .isInstanceOf(DependencyCycleException.class);

        // 数据库里不应留下这条边
        assertThat(deps.prerequisitesOf(c)).isEmpty();
    }

    @Test
    @DisplayName("重复添加同一条依赖是幂等的")
    void 重复添加幂等() {
        String a = task("A");
        String b = task("B");

        deps.addDependency(a, b);
        deps.addDependency(a, b);
        deps.addDependency(a, b);

        assertThat(deps.prerequisitesOf(a)).containsExactly(b);
    }

    @Test
    @DisplayName("全图环检测：正常 DAG 返回空")
    void 全图检测对DAG返回空() {
        String a = task("A"), b = task("B"), c = task("C"), d = task("D");
        deps.addDependency(a, b);
        deps.addDependency(b, c);
        deps.addDependency(c, d);

        assertThat(deps.findAnyCycle()).isEmpty();
    }

    @Test
    @DisplayName("删除依赖后可以完成")
    void 删除依赖后可完成() {
        String a = task("A"), b = task("B");
        deps.addDependency(a, b);

        assertThatThrownBy(() -> tasks.complete(a, 0L))
            .isInstanceOf(DependencyViolationException.class);

        deps.removeDependency(a, b);

        assertThat(tasks.complete(a, 0L).getStatus().name()).isEqualTo("DONE");
    }

    @Test
    @DisplayName("下游跟着动：B 完成后 A 的状态流独立")
    void 链式依赖() {
        String a = task("最终交付");
        String b = task("中间步骤");
        deps.addDependency(a, b);

        tasks.complete(b, 0L);
        assertThat(tasks.getOrThrow(b).getStatus().name()).isEqualTo("DONE");
        assertThat(tasks.getOrThrow(a).getStatus().name()).isEqualTo("TODO");
    }
}
