package io.cadence.core.web;

import io.cadence.core.domain.Recurrence;
import io.cadence.core.service.ConflictService;
import io.cadence.core.service.TaskService;
import io.cadence.core.web.dto.BusyResponse;
import io.cadence.core.web.dto.OccurrenceResponse;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1")
public class CalendarController {

    private final ConflictService conflictService;
    private final TaskService taskService;

    public CalendarController(ConflictService conflictService, TaskService taskService) {
        this.conflictService = conflictService;
        this.taskService = taskService;
    }

    /**
     * 忙闲查询。返回 [from, to] 内已被占用、且不可再安排的时间段（已合并相邻段）。
     *
     * Agent 的重排期求解器直接消费这个结果。
     */
    @GetMapping("/calendar/busy")
    public BusyResponse busy(
            @RequestParam @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime from,
            @RequestParam @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime to) {

        var windows = conflictService.busyRanges(from, to).stream()
            .map(w -> new BusyResponse.Window(w.start(), w.end(), w.durationMinutes()))
            .toList();
        return new BusyResponse(windows);
    }

    /**
     * 展开重复任务在 [from, to] 内的具体实例时间点。
     */
    @GetMapping("/tasks/{id}/occurrences")
    public OccurrenceResponse occurrences(
            @PathVariable String id,
            @RequestParam @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime from,
            @RequestParam @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime to,
            @RequestParam(defaultValue = "200") int cap) {

        // 空 id 视为"不存在"，而不是让后续解析崩溃 —— Agent 传空串是常见幻觉形态之一
        if (id == null || id.isBlank()) {
            throw new io.cadence.core.service.TaskNotFoundException(String.valueOf(id));
        }

        var task = taskService.getOrThrow(id);
        if (task.getRrule() == null || task.getRrule().isBlank()) {
            // 非重复任务：只有它自己的时间点
            OffsetDateTime anchor = task.getStartAt() != null ? task.getStartAt() : task.getDueAt();
            var single = (anchor != null && !anchor.isBefore(from) && !anchor.isAfter(to))
                ? java.util.List.of(anchor)
                : java.util.List.<OffsetDateTime>of();
            return new OccurrenceResponse(id, null, from, to, single, false);
        }

        OffsetDateTime anchor = task.getStartAt() != null ? task.getStartAt() : task.getDueAt();
        if (anchor == null) {
            return new OccurrenceResponse(id, task.getRrule(), from, to, java.util.List.of(), false);
        }

        int safeCap = Math.clamp(cap, 1, 500);
        var occurrences = Recurrence.parse(task.getRrule()).expand(anchor, from, to, safeCap)
            .stream()
            .map(dt -> dt.atZoneSameInstant(ZoneId.of("Asia/Shanghai")).toOffsetDateTime())
            .toList();
        return new OccurrenceResponse(
            id, task.getRrule(), local(from), local(to), occurrences, occurrences.size() >= safeCap);
    }

    private static OffsetDateTime local(OffsetDateTime t) {
        return t == null ? null : t.atZoneSameInstant(ZoneId.of("Asia/Shanghai")).toOffsetDateTime();
    }
}
