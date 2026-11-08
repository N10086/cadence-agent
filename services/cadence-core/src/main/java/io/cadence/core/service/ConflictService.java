package io.cadence.core.service;

import io.cadence.core.domain.Priority;
import io.cadence.core.domain.Task;
import io.cadence.core.domain.TaskStatus;
import io.cadence.core.domain.TimeWindow;
import io.cadence.core.repository.TaskRepository;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 冲突检测与忙闲查询。
 *
 * 冲突检测是"提示"而非"阻止"：PRD 里明确返回 200 + warnings[]，
 * 因为用户可能确实想把两个任务放在同一时段（例如"边开会边写文档"）。
 * 阻止会让 Agent 陷入无意义的纠错循环。
 */
@Service
public class ConflictService {

    private final TaskRepository taskRepo;

    public ConflictService(TaskRepository taskRepo) {
        this.taskRepo = taskRepo;
    }

    /**
     * 找出与给定任务时间重叠的其他任务。
     *
     * 任务占用的时段 = [startAt, startAt + estimateMin)，或 [dueAt - estimateMin, dueAt)。
     * 两者都没填的任务不参与冲突检测。
     */
    @Transactional(readOnly = true)
    public List<Conflict> detect(String excludeTaskId, OffsetDateTime startAt, OffsetDateTime dueAt,
                                 Integer estimateMin) {
        TimeWindow window = occupiedWindow(startAt, dueAt, estimateMin);
        if (window == null) {
            return List.of();
        }

        List<Task> candidates = taskRepo.findAll(
            io.cadence.core.repository.TaskSpecifications
                .byFilter(null, null, null, null, null, null, null));

        List<Conflict> conflicts = new ArrayList<>();
        for (Task t : candidates) {
            if (t.getId().equals(excludeTaskId)) continue;
            if (t.getStatus() == TaskStatus.DONE || t.getStatus() == TaskStatus.CANCELLED) continue;

            TimeWindow other = occupiedWindow(t.getStartAt(), t.getDueAt(), t.getEstimateMin());
            if (other == null) continue;

            TimeWindow inter = window.intersection(other);
            if (inter != null) {
                conflicts.add(new Conflict(
                    t.getId(), t.getTitle(), t.getPriority(),
                    other.start(), other.end(), inter.durationMinutes()));
            }
        }
        conflicts.sort(Comparator.comparing(Conflict::overlapStart));
        return conflicts;
    }

    /** 计算任务实际占用的时间窗口，两个来源都没填则返回 null */
    public static TimeWindow occupiedWindow(OffsetDateTime startAt, OffsetDateTime dueAt, Integer estimateMin) {
        if (estimateMin == null || estimateMin <= 0) {
            return null;
        }
        if (startAt != null) {
            return TimeWindow.of(startAt, startAt.plusMinutes(estimateMin));
        }
        if (dueAt != null) {
            return TimeWindow.of(dueAt.minusMinutes(estimateMin), dueAt);
        }
        return null;
    }

    /**
     * 忙闲查询：返回 [from, to] 内已被占用且不可再安排的时间段。
     *
     * 当前实现只考虑已有任务的占用。会议、通勤等外部忙碌
     * 由 Agent 侧的 working_hours 配置补充（见 PRD §5 求解器输入）。
     */
    @Transactional(readOnly = true)
    public List<TimeWindow> busyRanges(OffsetDateTime from, OffsetDateTime to) {
        List<Task> all = taskRepo.findAll(
            io.cadence.core.repository.TaskSpecifications
                .byFilter(null, null, null, null, null, null, null));

        List<TimeWindow> ranges = new ArrayList<>();
        for (Task t : all) {
            if (t.getStatus() == TaskStatus.DONE || t.getStatus() == TaskStatus.CANCELLED) continue;
            TimeWindow w = occupiedWindow(t.getStartAt(), t.getDueAt(), t.getEstimateMin());
            if (w == null) continue;

            TimeWindow clipped = w.intersection(TimeWindow.of(from, to));
            if (clipped != null) {
                ranges.add(clipped);
            }
        }

        ranges.sort(Comparator.comparing(TimeWindow::start));
        return merge(ranges);
    }

    /** 合并重叠或相接的区间 */
    static List<TimeWindow> merge(List<TimeWindow> sorted) {
        if (sorted.isEmpty()) return List.of();

        List<TimeWindow> out = new ArrayList<>();
        TimeWindow cur = sorted.get(0);
        for (int i = 1; i < sorted.size(); i++) {
            TimeWindow next = sorted.get(i);
            if (!next.start().isAfter(cur.end())) {   // 重叠或相接
                cur = new TimeWindow(cur.start(),
                    next.end().isAfter(cur.end()) ? next.end() : cur.end());
            } else {
                out.add(cur);
                cur = next;
            }
        }
        out.add(cur);
        return out;
    }

    public record Conflict(
        String taskId,
        String title,
        Priority priority,
        OffsetDateTime overlapStart,
        OffsetDateTime overlapEnd,
        long overlapMinutes
    ) {}
}
