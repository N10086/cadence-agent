package io.cadence.core.web;

import com.fasterxml.jackson.annotation.JsonInclude;

/**
 * 统一响应包装：{@code {code, message, data}}。
 *
 * 这是一个纯信封，不解释自己装了什么 —— 真正的序列化由
 * {@link ApiResponseAdvice} 负责，它把 Controller 的原始返回值塞进 data。
 *
 * 约定（与 docs/接口文档.md 第三章一致）：
 *   - code = 0   → 成功
 *   - code &gt; 0 → 失败，code 等于对应的 HTTP 状态码（400/403/404/409/422/500）
 *   - HTTP 状态码本身也如实反映成败，前端只看 status 或只看 code 都能判断
 *   - data 为 null 时不输出该字段；除 204 外的成功响应都有 data
 *
 * 为什么 code 成功用 0 而不是 1：
 *   同一份文档同时服务浏览器和 Agent。0 是「无错误」的通用约定，
 *   1 容易被误读成「多了一个可恢复错误」或者「布尔 true」。
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
public record ApiResponse<T>(int code, String message, T data) {

    public static final int SUCCESS_CODE = 0;
    public static final String SUCCESS_MESSAGE = "success";

    public static <T> ApiResponse<T> ok(T data) {
        return new ApiResponse<>(SUCCESS_CODE, SUCCESS_MESSAGE, data);
    }

    public static <T> ApiResponse<T> fail(int code, String message) {
        return new ApiResponse<>(code, message, null);
    }
}
