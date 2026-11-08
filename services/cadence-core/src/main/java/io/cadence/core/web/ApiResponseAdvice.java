package io.cadence.core.web;

import io.cadence.core.web.error.ApiError;
import org.springframework.core.MethodParameter;
import org.springframework.http.MediaType;
import org.springframework.http.converter.HttpMessageConverter;
import org.springframework.http.server.ServerHttpRequest;
import org.springframework.http.server.ServerHttpResponse;
import org.springframework.http.server.ServletServerHttpRequest;
import org.springframework.http.server.ServletServerHttpResponse;
import org.springframework.web.ErrorResponse;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.servlet.mvc.method.annotation.ResponseBodyAdvice;

/**
 * 统一响应包装：{@code {code, message, data}} 的落地点。
 *
 * 两条路径都必须经过这里，否则前端会同时收到「有包装」和「没包装」两种格式：
 *
 *  1. 正常返回 —— Controller 的原始返回值（TaskResponse / Map / List ...），
 *     在 {@link #beforeBodyWrite} 里包成 {code:0, message:"success", data:...}
 *
 *  2. 错误返回 —— 错误体可能是两种形态：
 *       a. GlobalExceptionHandler 返回的 ResponseEntity&lt;ApiError&gt;（业务异常的出口）
 *       b. Spring 内置处理器产生的 ErrorResponse（无路由、405、参数类型不匹配……）
 *     两者都在 {@link #beforeBodyWrite} 里被识别并重写成 {code, message}
 *
 * 这样设计的原因：错误形态是「返回值」而不是「异常」，
 * 用 beforeBodyWrite 一次性覆盖「正常 + 业务错误 + 内置错误」，没有哪条路径能漏网。
 *
 * 防二次包装：只在「不是 ApiResponse」时才包，所以幂等。
 *
 * 作用范围：只包 /api/** 下的业务接口。springdoc 的 /v3/api-docs、actuator 的 /actuator/health
 * 各有自己的格式契约，一旦被包，工具链会一起坏掉。
 */
@RestControllerAdvice
public class ApiResponseAdvice implements ResponseBodyAdvice<Object> {

    @Override
    public boolean supports(MethodParameter returnType, Class<? extends HttpMessageConverter<?>> converterType) {
        // 只管 JSON 响应；其余体（纯文本、字节流）不碰。
        // 注意：Boot 4 的 JSON 转换器是 JacksonJsonHttpMessageConverter（Jackson 3），
        // 它不继承 AbstractJackson2HttpMessageConverter —— 这里必须按名字判断，
        // 否则 supports() 恒为 false，包装会静默失效。
        String name = converterType.getName();
        return name.contains("Jackson") || name.contains("JsonHttpMessageConverter");
    }

    @Override
    public Object beforeBodyWrite(Object body,
                                  MethodParameter returnType,
                                  MediaType selectedContentType,
                                  Class<? extends HttpMessageConverter<?>> selectedConverterType,
                                  ServerHttpRequest request,
                                  ServerHttpResponse response) {

        // 应用自己的业务接口才有统一格式；springdoc / actuator 保持它们本来的样子
        if (!(request instanceof ServletServerHttpRequest servlet) || !isBusinessApi(servlet)) {
            return body;
        }
        int status = statusOf(response);

        // 204 / 205 / 304：协议上不允许带 body
        if (body == null || status == 204 || status == 205 || status == 304) {
            return body;
        }
        // 已经包装过（例如某处显式返回了 ApiResponse）—— 幂等，不重复包
        if (body instanceof ApiResponse<?>) {
            return body;
        }
        // 业务错误：GlobalExceptionHandler 产出的 ApiError
        if (body instanceof ApiError error) {
            return ApiResponse.fail(status >= 400 ? status : error.httpStatus(), error.message());
        }
        // Spring 内置错误：ErrorResponse 家族（它的 body 就是 ProblemDetail）
        if (body instanceof ErrorResponse || body instanceof org.springframework.http.ProblemDetail) {
            return ApiResponse.fail(status, messageOf(body));
        }
        return ApiResponse.ok(body);
    }

    // ------------------------------------------------------------------

    private static String messageOf(Object body) {
        if (body instanceof ErrorResponse ex) {
            try {
                var detail = ex.getBody();
                return detail == null || detail.getDetail() == null ? "请求无法处理" : detail.getDetail();
            } catch (RuntimeException e) {
                return "请求无法处理";
            }
        }
        if (body instanceof org.springframework.http.ProblemDetail pd) {
            return pd.getDetail() == null ? "请求无法处理" : pd.getDetail();
        }
        return "请求无法处理";
    }

    /**
     * 只包装应用自己的接口。
     *
     * 反例：springdoc 的 /v3/api-docs、actuator 的 /actuator/health 也有自己的格式契约，
     * 若被包一层，OpenApiExportIT 和其它工具链会同时坏掉。
     */
    private static boolean isBusinessApi(ServletServerHttpRequest request) {
        String ctx = request.getServletRequest().getContextPath();
        String path = request.getServletRequest().getRequestURI();
        if (ctx != null && !ctx.isEmpty() && path.startsWith(ctx)) {
            path = path.substring(ctx.length());
        }
        return path.startsWith("/api/");
    }

    private static int statusOf(ServerHttpResponse response) {
        if (response instanceof ServletServerHttpResponse served) {
            return served.getServletResponse().getStatus();
        }
        return 200;
    }
}
