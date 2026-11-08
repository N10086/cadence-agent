package io.cadence.core.domain;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;

/**
 * 示例：纯 JUnit 5 + AssertJ 的单元测试（不启动 Spring、不需要 Docker）。
 *
 * 只做一件事 —— 说明"有没有 JUnit、能不能跑"这个问题：
 * 任何以 Test 结尾的类都会被 surefire 识别，无需额外配置。
 */
class PriorityTest {

    @Test
    @DisplayName("优先级枚举包含 PRD 约定的四档")
    void 四档优先级() {
        assertThat(Priority.values()).hasSize(4);
    }

    @ParameterizedTest(name = "{0}")
    @EnumSource(Priority.class)
    @DisplayName("每个优先级都能往返序列化")
    void 名称往返(Priority p) {
        assertThat(Priority.valueOf(p.name())).isSameAs(p);
    }
}
