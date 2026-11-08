package io.cadence.core.web.error;

import com.fasterxml.jackson.annotation.JsonInclude;
import java.util.List;
import java.util.Map;

/**
 * 业务错误体 —— 是「内部结构」，不出现在最终 HTTP 响应里。
 *
 * 最终响应统一是 ApiResponse：{@code {code, message, data}}，由
 * {@code io.cadence.core.web.ApiResponseAdvice} 完成转换：
 *   code    = 这里的 code（ErrorCode 名称），映射为数字 / HTTP 状态码
 *   message = 这里的 message（人话）
 *   data    = null
 *
 * recoverable / suggestions / details 保留在这里，供服务端日志、trace 与 Agent 决策使用。
 * 对接口文档的读者而言，只需要关心 HTTP 状态码 + 数字 code + message。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record ApiError(
    /** 机器可读错误类型，见 ErrorCode */
    String code,
    /** 面向人（和模型）的说明 */
    String message,
    /** 调用方能否通过改变请求内容自修复；false 表示重试永远不会成功 */
    boolean recoverable,
    /** 给模型的可行动建议，例如 [{tool: task.search, args: {...}}] */
    List<Map<String, Object>> suggestions,
    /** 失败时的附加诊断信息 */
    List<Map<String, Object>> details
) {

    public static ApiError of(ErrorCode code, String message) {
        return new ApiError(code.name(), message, isRecoverable(code), null, null);
    }

    public static ApiError of(ErrorCode code, String message, List<Map<String, Object>> suggestions) {
        return new ApiError(code.name(), message, isRecoverable(code), suggestions, null);
    }

    public static ApiError of(ErrorCode code, String message,
                              List<Map<String, Object>> suggestions,
                              List<Map<String, Object>> details) {
        return new ApiError(code.name(), message, isRecoverable(code), suggestions, details);
    }

    private static boolean isRecoverable(ErrorCode code) {
        return switch (code) {
            // 改请求内容后可能成功
            case VALIDATION_ERROR, CONFLICT, NOT_FOUND, CONFIRM_REQUIRED -> true;
            // 重试同样的请求永远不会成功，必须由用户改变意图
            case ILLEGAL_TRANSITION, DEPENDENCY_VIOLATION, INTERNAL_ERROR -> false;
        };
    }

    /** 对应的 HTTP 状态码，ApiResponseAdvice 用它填 ApiResponse.code。 */
    public int httpStatus() {
        return ErrorCode.valueOf(code).httpStatus();
    }
}
