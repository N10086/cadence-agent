package io.cadence.core.web.dto;

import jakarta.validation.constraints.NotBlank;

public record DependencyRequest(@NotBlank String dependsOnId) {}
