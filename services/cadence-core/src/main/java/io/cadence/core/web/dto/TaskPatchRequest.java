package io.cadence.core.web.dto;

import io.cadence.core.domain.Priority;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import java.time.OffsetDateTime;
import java.util.List;

/**
 * 局部更新请求。
 *
 * version 必填 —— 这是乐观锁的锚点，也是幻觉检测器：
 * 编造的 id 往往配着编造的 version，一次 409 就暴露了。
 *
 * 字段为 null 表示"不修改"。若确实要把某字段清空，
 * 用 patch 里的显式 null 语义（当前版本暂不支持清空，W3 再加）。
 */
public record TaskPatchRequest(
    @NotNull Long version,
    @Size(max = 200) String title,
    String description,
    Priority priority,
    OffsetDateTime startAt,
    OffsetDateTime dueAt,
    @Min(5) @Max(480) Integer estimateMin,
    Boolean pinned,
    @Size(max = 200) String rrule,
    List<String> tags
) {}
