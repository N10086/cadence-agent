package io.cadence.core.web.dto;

import java.time.OffsetDateTime;
import java.util.List;

public record BusyResponse(List<Window> windows) {

    public record Window(OffsetDateTime start, OffsetDateTime end, long minutes) {}
}
