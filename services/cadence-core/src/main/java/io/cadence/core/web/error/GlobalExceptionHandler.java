package io.cadence.core.web.error;

import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.orm.ObjectOptimisticLockingFailureException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import io.cadence.core.service.TaskNotFoundException;
import io.cadence.core.web.ApiResponse;
import io.cadence.core.web.error.BatchConflictException;
import io.cadence.core.web.error.DependencyViolationException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 业务异常 → 内部 ApiError → ApiResponseAdvice 统一包装成 {code,message,data}。
 *
 * 本类不直接产出对外 JSON：{@code ResponseEntity<ApiError>} 会被
 * {@link io.cadence.core.web.ApiResponseAdvice} 转换。
 * 之所以保留 ApiError 这一层：recoverable / suggestions / details 是给日志、
 * trace 和 Agent 决策用的，对外只暴露 code + message。
 *
 * 例外：ConfirmRequiredException 直接返回 {@code ResponseEntity<ApiResponse>}，
 * 因为 403 的响应体里要给前端「下一步要用的确认令牌」，不只是错误信息。
 *
 * 特别注意 ObjectOptimisticLockingFailureException：
 * 不显式处理的话 Spring 默认返回 500，而 Agent 的 core_client 是按 409 判断"可恢复"的，
 * 500 会被当成不可恢复，直接放弃整个操作。
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<ApiError> onValidation(MethodArgumentNotValidException ex) {
        List<Map<String, Object>> details = ex.getBindingResult().getFieldErrors().stream()
            .map(fe -> Map.<String, Object>of("field", fe.getField(), "reason", String.valueOf(fe.getDefaultMessage())))
            .toList();
        // message 带上第一个出错字段，前端和模型都能直接看懂哪里错了
        String reason = ex.getBindingResult().getFieldErrors().stream()
            .findFirst()
            .map(fe -> fe.getField() + " " + fe.getDefaultMessage())
            .orElse("请求参数不合法");
        return ResponseEntity.badRequest()
            .body(ApiError.of(ErrorCode.VALIDATION_ERROR, "请求参数不合法: " + reason, null, details));
    }

    @ExceptionHandler(VersionConflictException.class)
    public ResponseEntity<ApiError> onVersionConflict(VersionConflictException ex) {
        return ResponseEntity.status(409)
            .body(ApiError.of(ErrorCode.CONFLICT, ex.getMessage(),
                List.of(Map.of("tool", "task.search", "hint", "重新获取最新 version 后重试")),
                List.of(Map.of("taskId", ex.getTaskId(),
                              "expectedVersion", ex.getExpected(),
                              "actualVersion", ex.getActual()))));
    }

    @ExceptionHandler(TaskNotFoundException.class)
    public ResponseEntity<ApiError> onNotFound(TaskNotFoundException ex) {
        return ResponseEntity.status(404)
            .body(ApiError.of(ErrorCode.NOT_FOUND, ex.getMessage(),
                List.of(Map.of("tool", "task.search", "hint", "先按关键词搜索，不要编造 id"))));
    }

    /**
     * 批量版本冲突：整批已回滚，applied=0。
     * 这个字段是给 Agent 看的——它必须知道"一个都没改成"，不能假设部分成功。
     */
    @ExceptionHandler(BatchConflictException.class)
    public ResponseEntity<ApiError> onBatchConflict(BatchConflictException ex) {
        return ResponseEntity.status(409)
            .body(ApiError.of(ErrorCode.CONFLICT, ex.getMessage(),
                List.of(Map.of("tool", "task.search", "hint", "重新获取最新 version 后重试整批")),
                ex.getFailedOps()));
    }

    @ExceptionHandler(io.cadence.core.web.error.DependencyCycleException.class)
    public ResponseEntity<ApiError> onCycle(io.cadence.core.web.error.DependencyCycleException ex) {
        return ResponseEntity.status(422)
            .body(ApiError.of(ErrorCode.DEPENDENCY_VIOLATION, ex.getMessage()));
    }

    @ExceptionHandler(io.cadence.core.web.error.DependencyViolationException.class)
    public ResponseEntity<ApiError> onDependency(DependencyViolationException ex) {
        return ResponseEntity.status(422)
            .body(ApiError.of(ErrorCode.DEPENDENCY_VIOLATION, ex.getMessage()));
    }

    /**
     * 请求体无法解析（非法 JSON、类型不匹配、日期格式错）。
     * 这是 400 而不是 500 —— 调用方（Agent）传了烂数据，必须能自修复。
     */
    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<ApiError> onUnreadable(HttpMessageNotReadableException ex) {
        String detail = ex.getMostSpecificCause() != null
            ? ex.getMostSpecificCause().getMessage()
            : "请求体不是合法 JSON";
        return ResponseEntity.badRequest()
            .body(ApiError.of(ErrorCode.VALIDATION_ERROR, "请求体无法解析: " + detail,
                null, List.of(Map.of("reason", detail))));
    }

    /**
     * 路径变量/查询参数类型转换失败（例如 OffsetDateTime 解析不了、id 传了空串）。
     * 同样是 400 而不是 500 —— 这是调用方的输入问题。
     */
    @ExceptionHandler(org.springframework.web.method.annotation.MethodArgumentTypeMismatchException.class)
    public ResponseEntity<ApiError> onTypeMismatch(
            org.springframework.web.method.annotation.MethodArgumentTypeMismatchException ex) {
        return ResponseEntity.badRequest()
            .body(ApiError.of(ErrorCode.VALIDATION_ERROR,
                "参数 " + ex.getName() + " 格式不合法: " + ex.getValue(), null,
                List.of(Map.of("param", ex.getName(),
                               "requiredType", String.valueOf(ex.getRequiredType())))));
    }

    /**
     * 路径不存在（Spring 6 起静态资源找不到抛的是 NoResourceFoundException）。
     *
     * 特别注意：它同时实现了 ErrorResponse，Spring 的顶级异常处理会把它当作
     * ErrorResponse 处理并带 404 状态码；所以这里必须返回 404，不能返 500。
     * 否则调错一个 URL 就会被当成服务端故障，前端会显示「服务器开小差了」。
     */
    @ExceptionHandler(org.springframework.web.servlet.resource.NoResourceFoundException.class)
    public ResponseEntity<ApiError> onNoResource(
            org.springframework.web.servlet.resource.NoResourceFoundException ex) {
        return ResponseEntity.status(ErrorCode.NOT_FOUND.httpStatus())
            .body(ApiError.of(ErrorCode.NOT_FOUND, "接口不存在: " + ex.getResourcePath()));
    }

    /** 不支持的方法（如对 GET 接口发 POST）。客户端问题，必须保持 405 并带 Allow 头。 */
    @ExceptionHandler(org.springframework.web.HttpRequestMethodNotSupportedException.class)
    public ResponseEntity<ApiError> onMethodNotAllowed(
            org.springframework.web.HttpRequestMethodNotSupportedException ex) {
        return ResponseEntity.status(405)
            .header("Allow", String.join(", ",
                ex.getSupportedHttpMethods() == null
                    ? java.util.List.of()
                    : ex.getSupportedHttpMethods().stream().map(Object::toString).toList()))
            .body(ApiError.of(ErrorCode.VALIDATION_ERROR,
                "不支持 " + ex.getMethod() + " 方法，请改用: " + ex.getSupportedHttpMethods()));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<ApiError> onIllegalArgument(IllegalArgumentException ex) {
        return ResponseEntity.status(400)
            .body(ApiError.of(ErrorCode.VALIDATION_ERROR, ex.getMessage()));
    }

    @ExceptionHandler(ObjectOptimisticLockingFailureException.class)
    public ResponseEntity<ApiError> onOptimisticLock(ObjectOptimisticLockingFailureException ex) {
        return ResponseEntity.status(409)
            .body(ApiError.of(ErrorCode.CONFLICT, "任务在操作期间被修改，请刷新后重试",
                List.of(Map.of("tool", "task.search", "hint", "重新获取最新 version 后重试"))));
    }

    @ExceptionHandler(IllegalTransitionException.class)
    public ResponseEntity<ApiError> onIllegalTransition(IllegalTransitionException ex) {
        return ResponseEntity.status(422)
            .body(ApiError.of(ErrorCode.ILLEGAL_TRANSITION, ex.getMessage(), null,
                List.of(Map.of("from", String.valueOf(ex.getFrom()),
                               "action", ex.getAction()))));
    }

    /**
     * 破坏性操作需要确认。两阶段删除的第一步。
     *
     * 与其它错误的区别：失败响应里带「下一步要用的东西」——
     * 确认令牌和任务快照。前端拿到后原样再发一次 DELETE 即可。
     * 所以这里返回 403，但 data 不是 null；统一外壳下它长这样：
     *   {"code":403,"message":"...","data":{"confirmToken":"ct_...","expiresInSeconds":300,"snapshot":{...}}}
     */
    @ExceptionHandler(ConfirmRequiredException.class)
    public ResponseEntity<ApiResponse<Map<String, Object>>> onConfirmRequired(ConfirmRequiredException ex) {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("confirmToken", ex.getConfirmToken());
        data.put("expiresInSeconds", 300);
        data.put("snapshot", ex.getSnapshot() == null ? Map.of() : ex.getSnapshot());

        return ResponseEntity.status(403)
            .body(new ApiResponse<>(403, ex.getMessage(), data));
    }

    @ExceptionHandler(IllegalStateException.class)
    public ResponseEntity<ApiError> onIllegalState(IllegalStateException ex) {
        return ResponseEntity.status(422)
            .body(ApiError.of(ErrorCode.ILLEGAL_TRANSITION,
                ex.getMessage() == null ? "状态不允许该操作" : ex.getMessage()));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<ApiError> onUnexpected(Exception ex) {
        // Spring 自己的协议异常（MethodArgumentTypeMismatch、HttpRequestMethodNotSupported、
        // NoResourceFound……）在上面都有专属 handler；这里只兜「真的没预料到」的异常，
        // 统一 500 且不把堆栈抛给调用方。真实排查靠 trace_id + 服务端日志。
        return ResponseEntity.status(500)
            .body(ApiError.of(ErrorCode.INTERNAL_ERROR, "服务内部错误"));
    }

    /**
     * 其它内置 ErrorResponse 子类（如框架内部抛出的 415/406 等）：沿用 Spring 算好的状态码。
     *
     * 关键点：状态码沿用原值，不能一律改成 500 —— 否则调错 URL 会被当成服务端故障。
     */
    @ExceptionHandler(org.springframework.web.ErrorResponseException.class)
    public ResponseEntity<ApiError> onErrorResponse(org.springframework.web.ErrorResponseException ex) {
        var status = ex.getStatusCode();
        String detail = ex.getBody() == null || ex.getBody().getDetail() == null
            ? "请求无法处理"
            : ex.getBody().getDetail();
        var code = status.value() == 404 ? ErrorCode.NOT_FOUND
            : status.value() >= 500 ? ErrorCode.INTERNAL_ERROR
            : ErrorCode.VALIDATION_ERROR;
        return ResponseEntity.status(status).body(ApiError.of(code, detail));
    }
}
