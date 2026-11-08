package io.cadence.core.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.OffsetDateTime;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * 半开区间 [start, end) 的边界测试。
 *
 * 这些语义会被冲突检测、忙闲查询、SP3 求解器同时消费；
 * 一个地方的边界写错，上层会表现为"看起来有冲突/看起来没冲突"的随机 bug。
 */
class TimeWindowBoundaryTest {

    private static OffsetDateTime t(String iso) {
        return OffsetDateTime.parse(iso);
    }

    @Test
    @DisplayName("构造：起点等于终点、终点早于起点都必须拒绝")
    void invalidConstruction() {
        assertThatThrownBy(() -> new TimeWindow(t("2026-11-04T09:00:00+08:00"),
            t("2026-11-04T09:00:00+08:00")))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("终点必须晚于起点");
        assertThatThrownBy(() -> new TimeWindow(t("2026-11-04T10:00:00+08:00"),
            t("2026-11-04T09:00:00+08:00")))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    @DisplayName("构造：null 起止必须拒绝")
    void nullConstruction() {
        assertThatThrownBy(() -> new TimeWindow(null, t("2026-11-04T09:00:00+08:00")))
            .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new TimeWindow(t("2026-11-04T09:00:00+08:00"), null))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    @DisplayName("相接不算重叠：[9,10) 与 [10,11) 不冲突")
    void touchingIsNotOverlap() {
        var a = TimeWindow.of(t("2026-11-04T09:00:00+08:00"), t("2026-11-04T10:00:00+08:00"));
        var b = TimeWindow.of(t("2026-11-04T10:00:00+08:00"), t("2026-11-04T11:00:00+08:00"));
        assertThat(a.overlaps(b)).isFalse();
        assertThat(b.overlaps(a)).isFalse();
        assertThat(a.intersection(b)).isNull();
        assertThat(b.intersection(a)).isNull();
    }

    @Test
    @DisplayName("包含关系：交集等于被包含的区间")
    void containmentIntersection() {
        var outer = TimeWindow.of(t("2026-11-04T09:00:00+08:00"), t("2026-11-04T17:00:00+08:00"));
        var inner = TimeWindow.of(t("2026-11-04T12:00:00+08:00"), t("2026-11-04T13:00:00+08:00"));
        assertThat(outer.intersection(inner)).isEqualTo(inner);
        assertThat(inner.intersection(outer)).isEqualTo(inner);
    }

    @Test
    @DisplayName("部分重叠：交集是交叠段，时长按分钟向下取整")
    void partialOverlap() {
        var a = TimeWindow.of(t("2026-11-04T09:00:00+08:00"), t("2026-11-04T10:30:00+08:00"));
        var b = TimeWindow.of(t("2026-11-04T10:00:00+08:00"), t("2026-11-04T11:00:00+08:00"));
        var inter = a.intersection(b);
        assertThat(inter).isEqualTo(
            TimeWindow.of(t("2026-11-04T10:00:00+08:00"), t("2026-11-04T10:30:00+08:00")));
        assertThat(inter.durationMinutes()).isEqualTo(30);
    }

    @Test
    @DisplayName("不同偏移表示同一 Instant：重叠判断应按 Instant，而不是字面时间")
    void offsetsCompareByInstant() {
        // 09:00+08:00 == 01:00Z
        var a = TimeWindow.of(t("2026-11-04T09:00:00+08:00"), t("2026-11-04T10:00:00+08:00"));
        var b = TimeWindow.of(t("2026-11-04T01:30:00Z"), t("2026-11-04T02:30:00Z"));
        assertThat(a.overlaps(b)).isTrue();
        assertThat(a.intersection(b).durationMinutes()).isEqualTo(30);
    }

    @Test
    @DisplayName("subtract：完全覆盖 → 空；部分覆盖 → 1 段；不重叠 → 原区间")
    void subtractEdges() {
        var base = TimeWindow.of(t("2026-11-04T09:00:00+08:00"), t("2026-11-04T10:00:00+08:00"));
        assertThat(base.subtract(TimeWindow.of(t("2026-11-04T08:00:00+08:00"),
            t("2026-11-04T11:00:00+08:00")))).isEmpty();
        assertThat(base.subtract(TimeWindow.of(t("2026-11-04T09:30:00+08:00"),
            t("2026-11-04T11:00:00+08:00"))))
            .containsExactly(TimeWindow.of(t("2026-11-04T09:00:00+08:00"),
                t("2026-11-04T09:30:00+08:00")));
        assertThat(base.subtract(TimeWindow.of(t("2026-11-04T11:00:00+08:00"),
            t("2026-11-04T12:00:00+08:00"))))
            .containsExactly(base);
    }

    @Test
    @DisplayName("subtract：扣掉中间一段 → 2 段（因为 TimeWindow 不允许空区间）")
    void subtractMiddle() {
        var base = TimeWindow.of(t("2026-11-04T09:00:00+08:00"), t("2026-11-04T12:00:00+08:00"));
        var rest = base.subtract(TimeWindow.of(t("2026-11-04T10:00:00+08:00"),
            t("2026-11-04T11:00:00+08:00")));
        assertThat(rest).containsExactly(
            TimeWindow.of(t("2026-11-04T09:00:00+08:00"), t("2026-11-04T10:00:00+08:00")),
            TimeWindow.of(t("2026-11-04T11:00:00+08:00"), t("2026-11-04T12:00:00+08:00")));
    }

    @Test
    @DisplayName("durationMinutes：不足 1 分钟按 0 计，30 秒不会进位成 1")
    void durationTruncatesSeconds() {
        var w = TimeWindow.of(t("2026-11-04T09:00:00+08:00"),
            t("2026-11-04T09:00:30+08:00"));
        assertThat(w.durationMinutes()).isZero();
    }
}
