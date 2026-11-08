package io.cadence.core.web.dto;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.NotNull;
import java.util.List;

/**
 * 批量更新请求。
 *
 * 语义：全部成功或全部回滚（由 @Transactional 保证）。
 * 每条都必须带 version —— 任何一条版本不符，整个批次回滚。
 *
 * 为什么必须由 Core 提供批量接口，而不是 Agent 循环调单条：
 * 循环调用会产生"改了 5 个、第 6 个失败、剩 3 个没改"的中间态，
 * 用户根本没法收拾。事务边界必须在 Core 侧。
 */
public record BatchUpdateRequest(
    @NotEmpty @Valid List<BatchOp> ops
) {

    public record BatchOp(
        @NotBlank String id,
        @NotNull Long version,
        @Valid Patch patch
    ) {}

    public record Patch(
        String title,
        String description,
        io.cadence.core.domain.Priority priority,
        java.time.OffsetDateTime startAt,
        java.time.OffsetDateTime dueAt,
        Integer estimateMin,
        Boolean pinned,
        String rrule,
        List<String> tags
    ) {}
}
