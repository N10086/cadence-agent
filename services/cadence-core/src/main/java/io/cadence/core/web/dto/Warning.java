package io.cadence.core.web.dto;

/**
 * 非致命的提示信息。
 *
 * 与 ApiError 的区别：
 *   ApiError  → 请求失败，有 HTTP 错误码
 *   Warning   → 请求成功（200/201），但有些事情你应该知道
 *
 * 典型场景：写入成功，但与其他任务时间冲突。
 */
public record Warning(
    String code,
    String message,
    String relatedTaskId
) {}
