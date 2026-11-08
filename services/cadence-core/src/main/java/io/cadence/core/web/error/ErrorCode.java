package io.cadence.core.web.error;

/**
 * 统一的业务错误码。
 *
 * 两级标识：
 *   - name()     —— 机器可读的类型名，用于服务端日志、trace、Agent 决策
 *   - httpStatus —— 对外 HTTP 状态码，也是 {code,message,data} 外壳里 code 的取值
 *
 * 对外只有数字 code：前端根据 HTTP 状态码 + message 就能
 * 显示可读的错误提示。给 Agent 的 recoverable/suggestions 仍在 ApiError 里，
 * 通过日志与 trace 传递。
 *
 * 多个错误类型可以共享一个状态码（例如 ILLEGAL_TRANSITION 与
 * DEPENDENCY_VIOLATION 都是 422），区分靠 message。
 */
public enum ErrorCode {

    /** 请求参数不合法（格式、范围、缺失） */
    VALIDATION_ERROR(400),
    /** 乐观锁冲突：version 不匹配 */
    CONFLICT(409),
    /** 目标不存在 */
    NOT_FOUND(404),
    /** 状态机不允许该流转 */
    ILLEGAL_TRANSITION(422),
    /** 依赖不满足（成环 / 前置未完成） */
    DEPENDENCY_VIOLATION(422),
    /** 需要确认令牌（两阶段删除的第一步） */
    CONFIRM_REQUIRED(403),
    /** 服务端未预期的错误 */
    INTERNAL_ERROR(500);

    private final int httpStatus;

    ErrorCode(int httpStatus) {
        this.httpStatus = httpStatus;
    }

    public int httpStatus() {
        return httpStatus;
    }
}
