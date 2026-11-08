package io.cadence.core.web.dto;

import java.time.OffsetDateTime;
import java.util.List;

public record OccurrenceResponse(
    String taskId,
    String rrule,
    OffsetDateTime from,
    OffsetDateTime to,
    List<OffsetDateTime> occurrences,
    boolean truncated
) {}
