package io.cadence.core.web.dto;

import com.fasterxml.jackson.annotation.JsonInclude;
import java.util.List;

/**
 * 列表响应。刻意带 has_more，让模型知道结果被截断了。
 * 永远不要把 200 条任务一次性塞进上下文。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record TaskListResponse(
    List<TaskResponse> items,
    boolean hasMore,
    int total
) {
    public static TaskListResponse of(List<TaskResponse> all, int limit) {
        boolean hasMore = all.size() > limit;
        List<TaskResponse> page = hasMore ? all.subList(0, limit) : all;
        return new TaskListResponse(page, hasMore, all.size());
    }
}
