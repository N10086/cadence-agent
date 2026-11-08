package io.cadence.core.web.dto;

import jakarta.validation.constraints.NotNull;

public record CompleteRequest(@NotNull Long version) {}
