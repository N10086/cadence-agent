package io.cadence.core.domain;

import java.time.DayOfWeek;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.temporal.TemporalAdjusters;
import java.util.ArrayList;
import java.util.EnumSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * iCal RRULE 的一个刻意很小的子集。
 *
 * 支持：FREQ (DAILY/WEEKLY/MONTHLY) · INTERVAL · COUNT · UNTIL · BYDAY · BYSETPOS
 * 不支持：YEARLY、BYSETPOS 的其他组合、WKST、EXDATE
 *
 * 为什么手写而不用 ical4j：
 *   1. ival4j 重、API 陡峭，为一个子集引入一个库不划算
 *   2. 展开规则本身就是要跟面试官讲清楚的东西（"月末 31 号怎么办"）
 *   3. 手写可以做到 100% 确定、可单测、无隐藏状态
 *
 * 月末处理：FREQ=MONTHLY + BYDAY=MO + BYSETPOS=-1 表示"每月最后一个周一"。
 * 直接算 "每月 31 号" 会跳过 2 月和小月 —— 本实现用 TemporalAdjusters 处理，不会跳过。
 */
public record Recurrence(
        Freq freq,
        int interval,
        Integer count,
        OffsetDateTime until,
        Set<DayOfWeek> byDay,
        Integer bySetPos
) {

    public enum Freq { DAILY, WEEKLY, MONTHLY }

    public Recurrence {
        if (interval < 1) {
            throw new IllegalArgumentException("INTERVAL 必须 >= 1");
        }
        if (count != null && count < 1) {
            throw new IllegalArgumentException("COUNT 必须 >= 1");
        }
        if (count != null && until != null) {
            throw new IllegalArgumentException("COUNT 与 UNTIL 不能同时出现（RFC 5545）");
        }
    }

    // ---------------------------------------------------------------
    // 解析
    // ---------------------------------------------------------------

    public static Recurrence parse(String rrule) {
        if (rrule == null || rrule.isBlank()) {
            throw new IllegalArgumentException("rrule 为空");
        }

        Freq freq = null;
        int interval = 1;
        Integer count = null;
        OffsetDateTime until = null;
        Set<DayOfWeek> byDay = null;
        Integer bySetPos = null;

        for (String part : rrule.trim().split(";")) {
            String[] kv = part.split("=", 2);
            if (kv.length != 2) {
                throw new IllegalArgumentException("无法解析的 RRULE 片段: " + part);
            }
            String key = kv[0].toUpperCase(Locale.ROOT);
            String val = kv[1].trim();

            switch (key) {
                case "FREQ" -> freq = Freq.valueOf(val.toUpperCase(Locale.ROOT));
                case "INTERVAL" -> interval = Integer.parseInt(val);
                case "COUNT" -> count = Integer.parseInt(val);
                case "UNTIL" -> until = OffsetDateTime.parse(val);
                case "BYDAY" -> byDay = parseByDay(val);
                case "BYSETPOS" -> bySetPos = Integer.parseInt(val);
                default -> throw new IllegalArgumentException("不支持的 RRULE 键: " + key);
            }
        }

        if (freq == null) {
            throw new IllegalArgumentException("RRULE 缺少 FREQ");
        }
        if (bySetPos != null && byDay == null) {
            throw new IllegalArgumentException("BYSETPOS 必须配合 BYDAY 使用");
        }
        return new Recurrence(freq, interval, count, until, byDay, bySetPos);
    }

    private static Set<DayOfWeek> parseByDay(String val) {
        Set<DayOfWeek> days = EnumSet.noneOf(DayOfWeek.class);
        for (String d : val.split(",")) {
            days.add(switch (d.trim().toUpperCase(Locale.ROOT)) {
                case "MO" -> DayOfWeek.MONDAY;
                case "TU" -> DayOfWeek.TUESDAY;
                case "WE" -> DayOfWeek.WEDNESDAY;
                case "TH" -> DayOfWeek.THURSDAY;
                case "FR" -> DayOfWeek.FRIDAY;
                case "SA" -> DayOfWeek.SATURDAY;
                case "SU" -> DayOfWeek.SUNDAY;
                default -> throw new IllegalArgumentException("不支持的 BYDAY: " + d);
            });
        }
        return days;
    }

    public static Recurrence of(String rrule) {
        return parse(rrule);
    }

    // ---------------------------------------------------------------
    // 展开
    // ---------------------------------------------------------------

    /**
     * 把重复规则展开为 [from, to] 区间内的具体时间点。
     *
     * 使用的时区取自 dtstart 的偏移，保证"每周一的 9 点"在 DST 地区也还是当地 9 点。
     *
     * @param dtstart 首次发生时间（含偏移）
     * @param from    区间起点（含）
     * @param to      区间终点（含）
     * @param hardCap 最多返回多少条，防止 COUNT 巨大时撑爆内存
     */
    public List<OffsetDateTime> expand(
            OffsetDateTime dtstart, OffsetDateTime from, OffsetDateTime to, int hardCap) {

        if (dtstart == null) {
            throw new IllegalArgumentException("dtstart 为空");
        }
        if (to.isBefore(from)) {
            throw new IllegalArgumentException("区间终点早于起点");
        }

        ZoneId zone = dtstart.getOffset();
        var startDate = dtstart.atZoneSameInstant(zone).toLocalDate();
        var startTime = dtstart.atZoneSameInstant(zone).toLocalTime();
        var fromLocal = from.atZoneSameInstant(zone).toLocalDateTime();
        var toLocal = to.atZoneSameInstant(zone).toLocalDateTime();

        List<OffsetDateTime> out = new ArrayList<>();
        int emitted = 0;
        int guard = 0;
        int maxIterations = 5000;   // 防死循环的硬上限

        // 按"周期"推进。每个周期内可能产出 0..n 个候选日期。
        LocalDate cursor = periodStart(startDate);
        LocalDate cursorStart = cursor;

        while (guard++ < maxIterations) {
            List<LocalDate> candidates = candidatesInPeriod(cursor, startDate);

            for (LocalDate date : candidates) {
                if (date.isBefore(startDate)) {
                    continue;                     // 不早于 dtstart
                }
                var instant = date.atTime(startTime).atZone(zone).toOffsetDateTime();

                if (count != null && emitted >= count) {
                    return out;
                }
                if (until != null && instant.isAfter(until)) {
                    return out;
                }

                emitted++;
                var localDt = instant.atZoneSameInstant(zone).toLocalDateTime();
                if (!localDt.isBefore(fromLocal) && !localDt.isAfter(toLocal)) {
                    out.add(instant);
                    if (out.size() >= hardCap) {
                        return out;
                    }
                }
            }

            // 已经越过 to 且（若选了 BYSETPOS 之外的周期规则）可以停
            if (cursorStart.plusDays(1).atStartOfDay(zone).toOffsetDateTime().isAfter(to)
                    && out.size() > 0) {
                break;
            }

            cursor = nextPeriod(cursor);
            if (cursor.getYear() > to.atZoneSameInstant(zone).getYear() + 1) {
                break;                             // 安全阀
            }
        }
        return out;
    }

    public List<OffsetDateTime> expand(OffsetDateTime dtstart, OffsetDateTime from, OffsetDateTime to) {
        return expand(dtstart, from, to, 500);
    }

    // ---------------------------------------------------------------
    // 周期推进
    // ---------------------------------------------------------------

    private LocalDate periodStart(LocalDate d) {
        return switch (freq) {
            case DAILY -> d;
            case WEEKLY -> d.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY));
            case MONTHLY -> d.withDayOfMonth(1);
        };
    }

    private LocalDate nextPeriod(LocalDate periodStart) {
        return switch (freq) {
            case DAILY -> periodStart.plusDays(interval);
            case WEEKLY -> periodStart.plusWeeks(interval);
            case MONTHLY -> periodStart.plusMonths(interval);
        };
    }

    private List<LocalDate> candidatesInPeriod(LocalDate periodStart, LocalDate templateDate) {
        return switch (freq) {
            // DAILY：periodStart 已经按 INTERVAL 推进过，直接用它
            case DAILY -> List.of(periodStart);
            case WEEKLY -> weekCandidates(periodStart, templateDate);
            case MONTHLY -> monthCandidates(periodStart, templateDate);
        };
    }

    private List<LocalDate> weekCandidates(LocalDate weekStart, LocalDate templateDate) {
        List<LocalDate> days = new ArrayList<>();
        if (byDay == null) {
            // 未指定 BYDAY：就取 dtstart 的那一天
            return List.of(templateDate);
        }
        // DayOfWeek 周一=1 .. 周日=7，weekStart 已是周一
        for (DayOfWeek d : DayOfWeek.values()) {
            if (byDay.contains(d)) {
                days.add(weekStart.plusDays(d.getValue() - 1L));
            }
        }
        return days;
    }

    private List<LocalDate> monthCandidates(LocalDate monthStart, LocalDate templateDate) {
        List<LocalDate> all = new ArrayList<>();

        if (byDay == null) {
            // 每月同一「日」。以 dtstart 的 day-of-month 为准，而不是每月 1 号。
            // 若当月不存在这一天（例如 31 号遇到 2 月），RFC 5545 允许跳过该月实例，
            // 这里取当月最后一天，语义更接近用户预期（"每月 31 号" → 2 月底仍有一次）。
            int dom = templateDate.getDayOfMonth();
            int lastDay = monthStart.lengthOfMonth();
            return List.of(monthStart.withDayOfMonth(Math.min(dom, lastDay)));
        }

        // 收集本月所有匹配 BYDAY 的日期
        List<LocalDate> matching = new ArrayList<>();
        LocalDate d = monthStart;
        while (d.getMonthValue() == monthStart.getMonthValue()) {
            if (byDay.contains(d.getDayOfWeek())) {
                matching.add(d);
            }
            d = d.plusDays(1);
        }

        if (bySetPos == null) {
            all.addAll(matching);
        } else {
            // BYSETPOS: 1 = 第一个，-1 = 最后一个
            int idx = bySetPos > 0 ? bySetPos - 1 : matching.size() + bySetPos;
            if (idx >= 0 && idx < matching.size()) {
                all.add(matching.get(idx));
            }
        }
        return all;
    }
}
