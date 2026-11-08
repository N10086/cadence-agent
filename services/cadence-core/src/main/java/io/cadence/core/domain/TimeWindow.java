package io.cadence.core.domain;

import java.time.OffsetDateTime;

/**
 * 一个半开时间区间 [start, end)。
 * 用来表示任务占用、忙碌时段、不可用窗口。
 */
public record TimeWindow(OffsetDateTime start, OffsetDateTime end) {

    public TimeWindow {
        if (start == null || end == null) {
            throw new IllegalArgumentException("时间窗口的起止不能为空");
        }
        if (!end.isAfter(start)) {
            throw new IllegalArgumentException("时间窗口的终点必须晚于起点");
        }
    }

    public static TimeWindow of(OffsetDateTime start, OffsetDateTime end) {
        return new TimeWindow(start, end);
    }

    public long durationMinutes() {
        return java.time.Duration.between(start, end).toMinutes();
    }

    /** 半开区间相交：a.start < b.end && b.start < a.end */
    public boolean overlaps(TimeWindow other) {
        return this.start.isBefore(other.end) && other.start.isBefore(this.end);
    }

    public TimeWindow intersection(TimeWindow other) {
        OffsetDateTime s = this.start.isAfter(other.start) ? this.start : other.start;
        OffsetDateTime e = this.end.isBefore(other.end) ? this.end : other.end;
        return !e.isAfter(s) ? null : new TimeWindow(s, e);
    }

    /** 从 this 中扣掉 other，返回 0-2 段剩余区间 */
    public java.util.List<TimeWindow> subtract(TimeWindow other) {
        if (!overlaps(other)) {
            return java.util.List.of(this);
        }
        java.util.List<TimeWindow> rest = new java.util.ArrayList<>(2);
        if (this.start.isBefore(other.start)) {
            rest.add(new TimeWindow(this.start, other.start));
        }
        if (this.end.isAfter(other.end)) {
            rest.add(new TimeWindow(other.end, this.end));
        }
        return rest;
    }
}
