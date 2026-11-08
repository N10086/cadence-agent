package io.cadence.core.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.DayOfWeek;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

/**
 * RRULE 展开的验收测试。
 *
 * 覆盖 PRD 里承诺的 FR-6：
 *   "支持 FREQ=DAILY/WEEKLY/MONTHLY、INTERVAL、COUNT、UNTIL、BYDAY；
 *    GET /occurrences 展开为具体时间点"
 *
 * 重点在月末：这是最容易写错、也最容易被面试官追问的地方。
 */
class RecurrenceTest {

    private static OffsetDateTime t(String iso) {
        return OffsetDateTime.parse(iso);
    }

    // ---------------------------------------------------------------
    @Nested
    @DisplayName("解析")
    class Parse {

        @Test
        void 解析基本字段() {
            var r = Recurrence.parse("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=10");
            assertThat(r.freq()).isEqualTo(Recurrence.Freq.WEEKLY);
            assertThat(r.interval()).isEqualTo(2);
            assertThat(r.count()).isEqualTo(10);
            assertThat(r.byDay()).containsExactlyInAnyOrder(DayOfWeek.MONDAY, DayOfWeek.WEDNESDAY);
        }

        @Test
        void 小写也能解析() {
            var r = Recurrence.parse("freq=daily");
            assertThat(r.freq()).isEqualTo(Recurrence.Freq.DAILY);
        }

        @Test
        void COUNT与UNTIL互斥() {
            assertThatThrownBy(() ->
                Recurrence.parse("FREQ=DAILY;COUNT=5;UNTIL=2026-12-01T00:00:00+08:00"))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("不能同时出现");
        }

        @Test
        void 不支持的键要报错() {
            assertThatThrownBy(() -> Recurrence.parse("FREQ=DAILY;BYMONTH=3"))
                .isInstanceOf(IllegalArgumentException.class);
        }

        @Test
        void 缺FREQ要报错() {
            assertThatThrownBy(() -> Recurrence.parse("INTERVAL=2"))
                .hasMessageContaining("缺少 FREQ");
        }
    }

    // ---------------------------------------------------------------
    @Nested
    @DisplayName("DAILY")
    class Daily {

        @Test
        void 每天一次_三天() {
            var r = Recurrence.of("FREQ=DAILY");
            List<OffsetDateTime> got = r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-04T00:00:00+08:00"),
                t("2026-11-06T23:59:59+08:00"));
            assertThat(got).containsExactly(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-05T09:00:00+08:00"),
                t("2026-11-06T09:00:00+08:00"));
        }

        @Test
        void INTERVAL为3() {
            var r = Recurrence.of("FREQ=DAILY;INTERVAL=3");
            List<OffsetDateTime> got = r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-04T00:00:00+08:00"),
                t("2026-11-20T00:00:00+08:00"));
            assertThat(got).hasSize(6)
                .containsExactly(
                    t("2026-11-04T09:00:00+08:00"),
                    t("2026-11-07T09:00:00+08:00"),
                    t("2026-11-10T09:00:00+08:00"),
                    t("2026-11-13T09:00:00+08:00"),
                    t("2026-11-16T09:00:00+08:00"),
                    t("2026-11-19T09:00:00+08:00"));
        }

        @Test
        void COUNT限制总数() {
            var r = Recurrence.of("FREQ=DAILY;COUNT=3");
            List<OffsetDateTime> got = r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-12-01T00:00:00+08:00"));
            assertThat(got).hasSize(3);
        }

        @Test
        void UNTIL截断() {
            var r = Recurrence.of("FREQ=DAILY;UNTIL=2026-11-06T10:00:00+08:00");
            List<OffsetDateTime> got = r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-12-01T00:00:00+08:00"));
            assertThat(got).hasSize(3);   // 4、5、6 三天
        }
    }

    // ---------------------------------------------------------------
    @Nested
    @DisplayName("WEEKLY")
    class Weekly {

        @Test
        void 每周一三五() {
            // 2026-11-04 是周三
            var r = Recurrence.of("FREQ=WEEKLY;BYDAY=MO,WE,FR");
            List<OffsetDateTime> got = r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-02T00:00:00+08:00"),
                t("2026-11-13T23:59:59+08:00"));
            assertThat(got).containsExactly(
                t("2026-11-04T09:00:00+08:00"),   // 本周三
                t("2026-11-06T09:00:00+08:00"),   // 本周五（周一已被 dtstart 排除）
                t("2026-11-09T09:00:00+08:00"),
                t("2026-11-11T09:00:00+08:00"),
                t("2026-11-13T09:00:00+08:00"));
        }

        @Test
        void 每两周一次() {
            var r = Recurrence.of("FREQ=WEEKLY;INTERVAL=2;BYDAY=WE");
            List<OffsetDateTime> got = r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-12-10T00:00:00+08:00"));
            assertThat(got).containsExactly(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-18T09:00:00+08:00"),
                t("2026-12-02T09:00:00+08:00"));
        }
    }

    // ---------------------------------------------------------------
    @Nested
    @DisplayName("MONTHLY")
    class Monthly {

        @Test
        @DisplayName("每月最后一个周一 —— 这是最容易写错的场景")
        void 每月最后一个周一() {
            // 2026-01 最后一个周一是 1/26
            var r = Recurrence.of("FREQ=MONTHLY;BYDAY=MO;BYSETPOS=-1");
            List<OffsetDateTime> got = r.expand(
                t("2026-01-26T09:00:00+08:00"),
                t("2026-01-01T00:00:00+08:00"),
                t("2026-06-30T23:59:59+08:00"));
            assertThat(got).containsExactly(
                t("2026-01-26T09:00:00+08:00"),  // 1月最后一个周一
                t("2026-02-23T09:00:00+08:00"),  // 2月（28天）
                t("2026-03-30T09:00:00+08:00"),
                t("2026-04-27T09:00:00+08:00"),
                t("2026-05-25T09:00:00+08:00"),
                t("2026-06-29T09:00:00+08:00"));
        }

        @Test
        void 每月第一个周五() {
            var r = Recurrence.of("FREQ=MONTHLY;BYDAY=FR;BYSETPOS=1");
            List<OffsetDateTime> got = r.expand(
                t("2026-11-06T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2027-01-31T23:59:59+08:00"));
            assertThat(got).containsExactly(
                t("2026-11-06T09:00:00+08:00"),
                t("2026-12-04T09:00:00+08:00"),
                t("2027-01-01T09:00:00+08:00"));
        }

        @Test
        @DisplayName("每月31号：2月自动跳过，不报错")
        void 每月31号跳过不存在的日期() {
            var r = Recurrence.of("FREQ=MONTHLY");
            List<OffsetDateTime> got = r.expand(
                t("2027-01-31T09:00:00+08:00"),
                t("2027-01-01T00:00:00+08:00"),
                t("2027-05-31T23:59:59+08:00"));
            // 1/31、2/28（跳过）、3/31、4/30（跳过）、5/31
            assertThat(got).containsExactly(
                t("2027-01-31T09:00:00+08:00"),
                t("2027-02-28T09:00:00+08:00"),
                t("2027-03-31T09:00:00+08:00"),
                t("2027-04-30T09:00:00+08:00"),
                t("2027-05-31T09:00:00+08:00"));
        }
    }

    // ---------------------------------------------------------------
    @Nested
    @DisplayName("时区与边界")
    class Timezone {

        @Test
        void 输出保留原始偏移() {
            var r = Recurrence.of("FREQ=DAILY;COUNT=2");
            List<OffsetDateTime> got = r.expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00"),
                t("2026-11-30T00:00:00+08:00"));
            assertThat(got).allSatisfy(
                dt -> assertThat(dt.getOffset()).isEqualTo(ZoneOffset.ofHours(8)));
        }

        @Test
        void 区间外的事件不返回() {
            var r = Recurrence.of("FREQ=DAILY");
            List<OffsetDateTime> got = r.expand(
                t("2026-11-01T09:00:00+08:00"),
                t("2026-11-10T00:00:00+08:00"),
                t("2026-11-12T23:59:59+08:00"));
            assertThat(got).containsExactly(
                t("2026-11-10T09:00:00+08:00"),
                t("2026-11-11T09:00:00+08:00"),
                t("2026-11-12T09:00:00+08:00"));
        }

        @Test
        void hardCap生效() {
            var r = Recurrence.of("FREQ=DAILY");
            List<OffsetDateTime> got = r.expand(
                t("2026-01-01T09:00:00+08:00"),
                t("2026-01-01T00:00:00+08:00"),
                t("2027-12-31T00:00:00+08:00"),
                10);
            assertThat(got).hasSize(10);
        }

        @Test
        void 终点早于起点报错() {
            assertThatThrownBy(() -> Recurrence.of("FREQ=DAILY").expand(
                t("2026-11-04T09:00:00+08:00"),
                t("2026-11-10T00:00:00+08:00"),
                t("2026-11-01T00:00:00+08:00")))
                .hasMessageContaining("区间终点早于起点");
        }

        @Test
        void BYSETPOS必须配合BYDAY() {
            assertThatThrownBy(() -> Recurrence.parse("FREQ=MONTHLY;BYSETPOS=-1"))
                .hasMessageContaining("BYSETPOS 必须配合 BYDAY");
        }
    }

    // ---------------------------------------------------------------
    @Test
    @DisplayName("确定性：同输入跑 10 次结果完全一致")
    void 确定性() {
        var r = Recurrence.of("FREQ=WEEKLY;BYDAY=MO,WE,FR;INTERVAL=2");
        var a = r.expand(t("2026-11-04T09:00:00+08:00"),
                         t("2026-11-01T00:00:00+08:00"),
                         t("2027-03-01T00:00:00+08:00"));
        for (int i = 0; i < 10; i++) {
            assertThat(r.expand(t("2026-11-04T09:00:00+08:00"),
                                t("2026-11-01T00:00:00+08:00"),
                                t("2027-03-01T00:00:00+08:00")))
                .isEqualTo(a);
        }
    }

    @Test
    void WeekdayEnum解析覆盖全部七天() {
        var r = Recurrence.parse("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU");
        assertThat(r.byDay()).hasSize(7);
        assertThat(r.byDay()).isEqualTo(Set.of(DayOfWeek.values()));
    }
}
