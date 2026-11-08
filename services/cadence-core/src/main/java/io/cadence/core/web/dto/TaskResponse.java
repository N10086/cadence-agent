package io.cadence.core.web.dto;

import com.fasterxml.jackson.annotation.JsonInclude;
import io.cadence.core.domain.Priority;
import io.cadence.core.domain.Task;
import io.cadence.core.domain.TaskStatus;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.util.List;

/**
 * 任务响应体。
 *
 * 时间字段统一转成 Asia/Shanghai 输出。
 * 理由：数据库存 UTC（timestamptz 的正确行为），但调用方读到的应该是本地语义，
 * 否则 Agent 看到 "10:00Z" 要自己换算，"下周三 18:00" 这类约束就更容易被搞错。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record TaskResponse(
    String id,
    String title,
    String description,
    TaskStatus status,
    Priority priority,
    OffsetDateTime startAt,
    OffsetDateTime dueAt,
    Integer estimateMin,
    String parentId,
    boolean pinned,
    String rrule,
    List<String> tags,
    Long version,
    OffsetDateTime createdAt,
    OffsetDateTime updatedAt
) {

    private static final ZoneId OUT = ZoneId.of("Asia/Shanghai");

    public static TaskResponse from(Task t) {
        return new TaskResponse(
            t.getId(), t.getTitle(), t.getDescription(),
            t.getStatus(), t.getPriority(),
            at(t.getStartAt()), at(t.getDueAt()), t.getEstimateMin(),
            t.getParentId(), t.isPinned(), t.getRrule(),
            t.getTags(), t.getVersion(),
            at(t.getCreatedAt()), at(t.getUpdatedAt())
        );
    }

    private static OffsetDateTime at(OffsetDateTime t) {
        return t == null ? null : t.atZoneSameInstant(OUT).toOffsetDateTime();
    }
}
