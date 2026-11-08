package io.cadence.core.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.DayOfWeek;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.time.format.DateTimeParseException;
import java.util.List;
import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

/**
 * RRULE 子集的边界测试集。
 *
 * 与 {@link RecurrenceTest} 的分工：
 * - RecurrenceTest 覆盖"正常路径"与 PRD 明确写出的例子；
 * - 本类只打边界：闭区间端点、COUNT/UNTIL 语义、容量上限、跨 DST、
 *   不支持的组合、以及会让展开器静默返回空结果的规模边界。
 *
 * 标了 {@code @Disabled("BUG-SP1-xx ...")} 的用例断言的是**规格要求的行为**，
 * 当前实现不满足；同名的 {@code bug_*} 用例锁定当前行为，修复后应一起改。
 */
class RecurrenceBoundaryTest {

    private static OffsetDateTime t(String iso) {
        return OffsetDateTime.parse(iso);
    }

    // ==================================================================
    @Nested
    @DisplayName("区间端点：闭区间语义")
    class IntervalEdges {

        @Test
        @DisplayName("from == to == 事件时刻时，事件仍应返回（闭区间）")
        void exactSingleInstant() {
            var r = Recurrence.of("FREQ=DAILY;COUNT=1");
            var got = r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-04T09:00:00+08:00"));
            assertThat(got).containsExactly(t("2026-11-04T09:00:00+08:00"));
        }

        @Test
        @DisplayName("from 恰好等于事件时刻，应包含；早一秒则不应包含")
        void fromBoundaryInclusive() {
            var r = Recurrence.of("FREQ=DAILY;COUNT=1");
            assertThat(r.expand(t("2026-11-04T09:00:00+08:00"),
                t("2026-11-04T09:00:00+08:00"), t("2026-11-05T00:00:00+08:00")))
                .hasSize(1);
            assertThat(r.expand(t("2026-11-04T09:00:00+08:00"),
                t("2026-11-04T09:00:01+08:00"), t("2026-11-05T00:00:00+08:00")))
                .isEmpty();
        }

        @Test
        @DisplayName("to 恰好等于事件时刻，应包含；早一秒则不应包含")
        void toBoundaryInclusive() {
            var r = Recurrence.of("FREQ=DAILY;COUNT=1");
            assertThat(r.expand(t("2026-11-04T09:00:00+08:00"),
                t("2026-11-04T00:00:00+08:00"), t("2026-11-04T09:00:00+08:00")))
                .hasSize(1);
            assertThat(r.expand(t("2026-11-04T09:00:00+08:00"),
                t("2026-11-04T00:00:00+08:00"), t("2026-11-04T08:59:59+08:00")))
                .isEmpty();
        }

        @Test
        @DisplayName("区间终点早于起点：必须显式报错，不能静默返回空")
        void invertedIntervalThrows() {
            assertThatThrownBy(() -> Recurrence.of("FREQ=DAILY").expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-10T00:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00")))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("区间终点早于起点");
        }
    }

    // ==================================================================
    @Nested
    @DisplayName("COUNT / UNTIL")
    class CountUntil {

        @Test
        @DisplayName("COUNT 统计的是从 dtstart 起的所有实例，不受查询窗口起点影响")
        void countIsAnchoredAtDtstartNotFrom() {
            var r = Recurrence.of("FREQ=DAILY;COUNT=3");
            // 前 3 次都在 11-01/02/03，查询从 11-10 开始 → 正确结果为空
            assertThat(r.expand(
                t("2026-11-01T09:00:00+08:00"),
                t("2026-11-10T00:00:00+08:00"),
                t("2026-11-30T00:00:00+08:00"))).isEmpty();
        }

        @Test
        @DisplayName("UNTIL 等于实例时刻时应包含（RFC 闭区间）")
        void untilIsInclusive() {
            var r = Recurrence.of("FREQ=DAILY;UNTIL=2026-11-06T09:00:00+08:00");
            assertThat(r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-11-30T00:00:00+08:00")))
                .containsExactly(
                    t("2026-11-04T09:00:00+08:00"),
                    t("2026-11-05T09:00:00+08:00"),
                    t("2026-11-06T09:00:00+08:00"));
        }

        @Test
        @DisplayName("UNTIL 早于 dtstart：第一次就应停止")
        void untilBeforeDtstart() {
            var r = Recurrence.of("FREQ=DAILY;UNTIL=2026-11-01T00:00:00+08:00");
            assertThat(r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-11-30T00:00:00+08:00"))).isEmpty();
        }

        @Test
        @DisplayName("UNTIL 与实例用不同偏移表示同一时刻，应按 Instant 比较")
        void untilComparesInstantsAcrossOffsets() {
            // 2026-11-06T01:00Z == 2026-11-06T09:00+08:00
            var r = Recurrence.of("FREQ=DAILY;UNTIL=2026-11-06T01:00:00Z");
            assertThat(r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-11-30T00:00:00+08:00"))).hasSize(3);
        }

        @Test
        @DisplayName("COUNT=1 且查询窗口完全在 dtstart 之前：空")
        void countOneBeforeWindow() {
            var r = Recurrence.of("FREQ=DAILY;COUNT=1");
            assertThat(r.expand(t("2026-11-10T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"), t("2026-11-09T00:00:00+08:00")))
                .isEmpty();
        }
    }

    // ==================================================================
    @Nested
    @DisplayName("MONTHLY 的月末与 BYSETPOS")
    class MonthlyEdges {

        @Test
        @DisplayName("每月 31 号遇到闰年 2 月：当前实现夹到 2/29")
        void monthly31ClampsToLeapDay() {
            var r = Recurrence.of("FREQ=MONTHLY");
            var got = r.expand(
                t("2028-01-31T09:00:00+08:00"),
                t("2028-01-01T00:00:00+08:00"),
                t("2028-04-30T23:59:59+08:00"));
            assertThat(got).containsExactly(
                t("2028-01-31T09:00:00+08:00"),
                t("2028-02-29T09:00:00+08:00"),
                t("2028-03-31T09:00:00+08:00"),
                t("2028-04-30T09:00:00+08:00"));
        }

        @Test
        @DisplayName("BYSETPOS=5 但当月只有 4 个周一：该月应没有实例，而不是回退到最后一个")
        void bySetPosOutOfRangeSkipsMonth() {
            var r = Recurrence.of("FREQ=MONTHLY;BYDAY=MO;BYSETPOS=5");
            var got = r.expand(
                t("2026-01-01T09:00:00+08:00"),
                t("2026-01-01T00:00:00+08:00"),
                t("2026-03-31T23:59:59+08:00"));
            // 2026-01 有 4 个周一、02 有 4 个、03 有 5 个（2/9/16/23/30）
            assertThat(got).containsExactly(t("2026-03-30T09:00:00+08:00"));
        }

        @Test
        @DisplayName("MONTHLY + BYDAY 不带 BYSETPOS：当月所有匹配日都展开")
        void monthlyByDayWithoutSetPos() {
            var r = Recurrence.of("FREQ=MONTHLY;BYDAY=MO");
            var got = r.expand(
                t("2026-01-26T09:00:00+08:00"),
                t("2026-01-26T00:00:00+08:00"),
                t("2026-02-28T23:59:59+08:00"));
            assertThat(got).containsExactly(
                t("2026-01-26T09:00:00+08:00"),
                t("2026-02-02T09:00:00+08:00"),
                t("2026-02-09T09:00:00+08:00"),
                t("2026-02-16T09:00:00+08:00"),
                t("2026-02-23T09:00:00+08:00"));
        }

        @Test
        @DisplayName("MONTHLY 跨年 INTERVAL=2：1/3/5/7/9/11 月，不应跳年")
        void monthlyIntervalAcrossYear() {
            var r = Recurrence.of("FREQ=MONTHLY;INTERVAL=2;COUNT=4");
            var got = r.expand(
                t("2026-11-15T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2027-07-01T00:00:00+08:00"));
            assertThat(got).containsExactly(
                t("2026-11-15T09:00:00+08:00"),
                t("2027-01-15T09:00:00+08:00"),
                t("2027-03-15T09:00:00+08:00"),
                t("2027-05-15T09:00:00+08:00"));
        }
    }

    // ==================================================================
    @Nested
    @DisplayName("规模与容量边界")
    class Capacity {

        @Test
        @DisplayName("hardCap=0 时仍返回 1 条：容量检查发生在 add 之后")
        void zeroCapReturnsOne() {
            var r = Recurrence.of("FREQ=DAILY");
            var got = r.expand(
                t("2026-11-01T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-11-30T00:00:00+08:00"),
                0);
            assertThat(got).hasSize(1);
        }

        @Test
        @DisplayName("hardCap 恰好等于区间内条数：返回 cap 条（调用方无法区分是否截断）")
        void capExactlyMatchesCount() {
            var r = Recurrence.of("FREQ=DAILY;COUNT=3");
            var got = r.expand(
                t("2026-11-01T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-11-30T00:00:00+08:00"),
                3);
            assertThat(got).hasSize(3);
        }

        @Test
        @Disabled("BUG-SP1-10：展开器从 dtstart 逐周期推进，maxIterations=5000；"
            + "dtstart 距 from 超过 5000 个周期时静默返回空结果，而不是快进到 from")
        @DisplayName("dtstart 在 2000 年、查询 2026 年：应返回最近 7 天，而不是空")
        void ancientDailyStartStillFindsRecentOccurrences() {
            var r = Recurrence.of("FREQ=DAILY");
            var got = r.expand(
                t("2000-01-01T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-11-07T23:59:59+08:00"));
            assertThat(got).hasSize(7);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-10 当前行为：远古 dtstart 的每日重复返回空")
        void bug_ancientDailyStartReturnsEmpty() {
            var r = Recurrence.of("FREQ=DAILY");
            var got = r.expand(
                t("2000-01-01T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-11-07T23:59:59+08:00"));
            assertThat(got).isEmpty();
        }

        @Test
        @DisplayName("最大迭代安全阀不应把 UNTIL 远期的规则误判成无实例（对照：dtstart 在窗口附近）")
        void normalDailyIsNotAffectedBySafetyValve() {
            var r = Recurrence.of("FREQ=DAILY;UNTIL=2030-01-01T00:00:00+08:00");
            assertThat(r.expand(
                t("2026-11-01T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-11-07T23:59:59+08:00"))).hasSize(7);
        }
    }

    // ==================================================================
    @Nested
    @DisplayName("时区与 DST")
    class TimezoneEdges {

        @Test
        @Disabled("BUG-SP1-11：实现用 dtstart 的固定 Offset 作为 ZoneId，"
            + "跨 DST 后墙钟时间会偏移；PRD 声称'在 DST 地区也还是当地 9 点'")
        @DisplayName("America/New_York 跨夏令时：每个实例都应是当地 09:00")
        void wallClockAcrossDst() {
            // 2026-03-08 02:00 美东进入夏令时
            var r = Recurrence.of("FREQ=DAILY;COUNT=10");
            var got = r.expand(
                t("2026-03-01T09:00:00-05:00"),
                t("2026-03-01T00:00:00-05:00"),
                t("2026-03-12T00:00:00-04:00"));
            ZoneId ny = ZoneId.of("America/New_York");
            assertThat(got).allSatisfy(dt ->
                assertThat(dt.atZoneSameInstant(ny).getHour()).isEqualTo(9));
        }

        @Test
        @DisplayName("锁定 BUG-SP1-11 当前行为：DST 之后当地墙钟变成 10:00")
        void bug_dstShiftsWallClock() {
            var r = Recurrence.of("FREQ=DAILY;COUNT=10");
            var got = r.expand(
                t("2026-03-01T09:00:00-05:00"),
                t("2026-03-01T00:00:00-05:00"),
                t("2026-03-12T00:00:00-04:00"));
            ZoneId ny = ZoneId.of("America/New_York");
            assertThat(got).anySatisfy(dt ->
                assertThat(dt.atZoneSameInstant(ny).getHour()).isEqualTo(10));
        }

        @Test
        @DisplayName("输出应保留 dtstart 的偏移（不强行改写成 UTC）")
        void preservesOffset() {
            var r = Recurrence.of("FREQ=DAILY;COUNT=2");
            assertThat(r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-11-30T00:00:00+08:00")))
                .allSatisfy(dt -> assertThat(dt.getOffset()).isEqualTo(ZoneOffset.ofHours(8)));
        }
    }

    // ==================================================================
    @Nested
    @DisplayName("解析的非法/歧义输入")
    class ParseEdges {

        @Test
        @DisplayName("空串、空白、只有分号：全部拒绝")
        void blankInputs() {
            assertThatThrownBy(() -> Recurrence.parse(null)).isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> Recurrence.parse("")).isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> Recurrence.parse("   ")).isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> Recurrence.parse("FREQ=DAILY;;COUNT=1"))
                .isInstanceOf(IllegalArgumentException.class);
        }

        @Test
        @DisplayName("UNTIL 只接受带偏移的日期时间；纯日期/UTC 基础格式会被拒绝")
        void untilFormats() {
            assertThatThrownBy(() -> Recurrence.parse("FREQ=DAILY;UNTIL=2026-12-01"))
                .isInstanceOf(DateTimeParseException.class);
            assertThatThrownBy(() -> Recurrence.parse("FREQ=DAILY;UNTIL=20261201T000000Z"))
                .isInstanceOf(DateTimeParseException.class);
        }

        @Test
        @DisplayName("INTERVAL/COUNT/BYSETPOS 非数字：拒绝")
        void numericFields() {
            assertThatThrownBy(() -> Recurrence.parse("FREQ=DAILY;INTERVAL=x"))
                .isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> Recurrence.parse("FREQ=DAILY;COUNT=1.5"))
                .isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> Recurrence.parse("FREQ=MONTHLY;BYDAY=MO;BYSETPOS=x"))
                .isInstanceOf(IllegalArgumentException.class);
        }

        @Test
        @DisplayName("重复键按最后一值生效（实现细节，非 RFC 要求）")
        void duplicateKeysLastWins() {
            var r = Recurrence.parse("FREQ=DAILY;INTERVAL=2;INTERVAL=3");
            assertThat(r.interval()).isEqualTo(3);
        }

        @Test
        @DisplayName("BYDAY 空值、未知值、大小写混合")
        void byDayFormats() {
            assertThatThrownBy(() -> Recurrence.parse("FREQ=WEEKLY;BYDAY="))
                .isInstanceOf(IllegalArgumentException.class);
            assertThatThrownBy(() -> Recurrence.parse("FREQ=WEEKLY;BYDAY=XX"))
                .isInstanceOf(IllegalArgumentException.class);
            assertThat(Recurrence.parse("freq=weekly;byday=mo,we").byDay())
                .containsExactlyInAnyOrder(DayOfWeek.MONDAY, DayOfWeek.WEDNESDAY);
        }

        @Test
        @DisplayName("缺少 FREQ、未知键、COUNT+UNTIL 同时出现：拒绝")
        void structuralErrors() {
            assertThatThrownBy(() -> Recurrence.parse("INTERVAL=2"))
                .hasMessageContaining("缺少 FREQ");
            assertThatThrownBy(() -> Recurrence.parse("FREQ=DAILY;BYMONTH=3"))
                .hasMessageContaining("不支持的 RRULE 键");
            assertThatThrownBy(() -> Recurrence.parse("FREQ=DAILY;COUNT=2;UNTIL=2026-12-01T00:00:00+08:00"))
                .hasMessageContaining("不能同时出现");
        }
    }

    // ==================================================================
    @Nested
    @DisplayName("被静默忽略的组合")
    class IgnoredCombinations {

        @Test
        @Disabled("BUG-SP1-12：BYSETPOS 只在 MONTHLY 分支生效；WEEKLY 下被静默忽略，"
            + "契约没有说明，模型/调用方会以为它生效了")
        @DisplayName("WEEKLY + BYSETPOS 应拒绝（或实现），不能静默忽略")
        void weeklyBySetPosRejectedOrApplied() {
            assertThatThrownBy(() -> Recurrence.parse("FREQ=WEEKLY;BYDAY=MO;BYSETPOS=1"))
                .isInstanceOf(IllegalArgumentException.class);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-12 当前行为：WEEKLY + BYSETPOS 被解析但展开结果与不带 BYSETPOS 相同")
        void bug_weeklyBySetPosIsIgnored() {
            var with = Recurrence.of("FREQ=WEEKLY;BYDAY=MO;BYSETPOS=1");
            var without = Recurrence.of("FREQ=WEEKLY;BYDAY=MO");
            var a = with.expand(t("2026-11-02T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"), t("2026-11-30T00:00:00+08:00"));
            var b = without.expand(t("2026-11-02T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"), t("2026-11-30T00:00:00+08:00"));
            assertThat(a).isEqualTo(b);
        }

        @Test
        @Disabled("BUG-SP1-12：DAILY + BYDAY 在实现里完全不参与候选日计算，被静默忽略")
        @DisplayName("DAILY + BYDAY=MO 应只在周一发生，不能每天发生")
        void dailyByDayShouldFilter() {
            var r = Recurrence.of("FREQ=DAILY;BYDAY=MO");
            var got = r.expand(t("2026-11-02T09:00:00+08:00"),
                t("2026-11-02T00:00:00+08:00"), t("2026-11-08T23:59:59+08:00"));
            assertThat(got).hasSize(1);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-12 当前行为：DAILY + BYDAY=MO 仍然每天展开")
        void bug_dailyByDayIgnored() {
            var r = Recurrence.of("FREQ=DAILY;BYDAY=MO");
            var got = r.expand(t("2026-11-02T09:00:00+08:00"),
                t("2026-11-02T00:00:00+08:00"), t("2026-11-08T23:59:59+08:00"));
            assertThat(got).hasSize(7);
        }
    }

    // ==================================================================
    @Test
    @DisplayName("dtstart 为 null：显式报错")
    void nullDtstartRejected() {
        assertThatThrownBy(() -> Recurrence.of("FREQ=DAILY").expand(null,
            t("2026-11-01T00:00:00+08:00"), t("2026-11-02T00:00:00+08:00")))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("dtstart");
    }

    @Test
    @DisplayName("INTERVAL 极大时不应死循环，且返回空而不是挂起")
    void hugeIntervalTerminates() {
        var r = Recurrence.of("FREQ=DAILY;INTERVAL=1000000;COUNT=2");
        List<OffsetDateTime> got = r.expand(
            t("2026-11-01T09:00:00+08:00"),
            t("2026-11-01T00:00:00+08:00"),
            t("2026-11-30T00:00:00+08:00"));
        assertThat(got).containsExactly(t("2026-11-01T09:00:00+08:00"));
    }
}
