package io.cadence.core.web.dto;

import java.time.OffsetDateTime;
import java.util.List;

public record TaskDetailResponse(
    TaskResponse task,
    List<String> dependsOn,
    List<String> blocks,
    List<ConflictView> conflicts
) {
    public record ConflictView(
        String taskId,
        String title,
        String priority,
        OffsetDateTime overlapStart,
        OffsetDateTime overlapEnd,
        long overlapMinutes
    ) {}
}
