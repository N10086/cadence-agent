package io.cadence.core.web.dto;

import io.cadence.core.domain.Priority;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import java.time.OffsetDateTime;
import java.util.List;

public record TaskCreateRequest(
    @NotBlank @Size(max = 200) String title,
    @Size(max = 4000) String description,
    Priority priority,
    OffsetDateTime startAt,
    OffsetDateTime dueAt,
    @Min(5) @Max(480) Integer estimateMin,
    String parentId,
    Boolean pinned,
    @Size(max = 200) String rrule,
    List<String> tags
) {}
