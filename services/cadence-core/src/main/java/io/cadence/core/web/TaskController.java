package io.cadence.core.web;

import io.cadence.core.domain.Priority;
import io.cadence.core.domain.TaskStatus;
import io.cadence.core.service.AuditService;
import io.cadence.core.service.ConfirmTokenService;
import io.cadence.core.service.ConflictService;
import io.cadence.core.service.DependencyService;
import io.cadence.core.service.IdempotencyService;
import io.cadence.core.service.TaskService;
import io.cadence.core.web.dto.CompleteRequest;
import io.cadence.core.web.dto.DependencyRequest;
import io.cadence.core.web.dto.TaskCreateRequest;
import io.cadence.core.web.dto.TaskDetailResponse;
import io.cadence.core.web.dto.TaskListResponse;
import io.cadence.core.web.dto.TaskPatchRequest;
import io.cadence.core.web.dto.TaskResponse;
import io.cadence.core.web.error.ConfirmRequiredException;
import jakarta.validation.Valid;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

/**
 * 任务控制器。
 *
 * 这里没有任何东西知道调用方是大语言模型。
 * 它只知道：有个客户端，带着 version、X-Actor、X-Trace-Id 来了。
 */
@RestController
@RequestMapping("/api/v1/tasks")
public class TaskController {

    private final TaskService taskService;
    private final DependencyService dependencyService;
    private final ConflictService conflictService;
    private final AuditService auditService;
    private final ConfirmTokenService confirmTokens;
    private final IdempotencyService idempotency;

    public TaskController(TaskService taskService,
                          DependencyService dependencyService,
                          ConflictService conflictService,
                          AuditService auditService,
                          ConfirmTokenService confirmTokens,
                          IdempotencyService idempotency) {
        this.taskService = taskService;
        this.dependencyService = dependencyService;
        this.conflictService = conflictService;
        this.auditService = auditService;
        this.confirmTokens = confirmTokens;
        this.idempotency = idempotency;
    }

    // ------------------------------------------------------------------
    // 查询
    // ------------------------------------------------------------------

    @GetMapping
    public TaskListResponse list(
            @RequestParam(required = false) String id,
            @RequestParam(required = false) String keyword,
            @RequestParam(required = false) TaskStatus status,
            @RequestParam(required = false) Priority priority,
            @RequestParam(required = false) OffsetDateTime dueBefore,
            @RequestParam(required = false) OffsetDateTime dueAfter,
            @RequestParam(required = false) String tag,
            @RequestParam(defaultValue = "20") int limit) {

        int safeLimit = Math.clamp(limit, 1, 50);
        var all = taskService.search(id, keyword, status, priority, dueBefore, dueAfter, tag)
            .stream().map(TaskResponse::from).toList();
        return TaskListResponse.of(all, safeLimit);
    }

    @GetMapping("/{id}")
    public TaskDetailResponse get(@PathVariable String id) {
        var task = taskService.getOrThrow(id);
        var conflicts = conflictService.detect(id, task.getStartAt(), task.getDueAt(), task.getEstimateMin());

        return new TaskDetailResponse(
            TaskResponse.from(task),
            dependencyService.prerequisitesOf(id),
            dependencyService.dependentsOf(id),
            conflicts.stream().map(c -> new TaskDetailResponse.ConflictView(
                c.taskId(), c.title(), c.priority().name(),
                c.overlapStart(), c.overlapEnd(), c.overlapMinutes())).toList()
        );
    }

    // ------------------------------------------------------------------
    // 写：创建（幂等）
    // ------------------------------------------------------------------

    /**
     * 创建任务。
     *
     * 带 X-Idempotency-Key 时：同键重复请求直接返回首次的完整响应，不产生重复数据。
     * 这是给"网络超时后重试"兜底的 —— Agent 每次工具调用都会自动生成幂等键。
     */
    @PostMapping
    public ResponseEntity<?> create(@Valid @RequestBody TaskCreateRequest req,
                                    @RequestHeader(value = "X-Actor", required = false) String actor,
                                    @RequestHeader(value = "X-Trace-Id", required = false) String traceId,
                                    @RequestHeader(value = "X-Idempotency-Key", required = false) String idemKey) {

        var existing = idempotency.find(idemKey);
        if (existing.isPresent()) {
            try {
                var mapper = new tools.jackson.databind.ObjectMapper();
                var node = mapper.readTree(existing.get().getResponse());
                // 重放：状态码仍是 201，但带 Idempotent-Replayed 标记，便于调用方区分
                return ResponseEntity.status(HttpStatus.CREATED)
                    .header("Idempotent-Replayed", "true")
                    .contentType(MediaType.APPLICATION_JSON)
                    .body(node);
            } catch (Exception e) {
                // 记录损坏就当作没有，走正常创建 —— 宁可多建一个，也不要 500
                idempotency.find(idemKey).ifPresent(r -> {});
            }
        }

        var task = taskService.create(req, actor, traceId);
        var response = TaskResponse.from(task);

        if (idemKey != null && !idemKey.isBlank()) {
            try {
                var mapper = new tools.jackson.databind.ObjectMapper();
                idempotency.save(idemKey, task.getId(), mapper.writeValueAsString(response));
            } catch (Exception ignored) {
                // 幂等记录写失败不应导致创建失败
            }
        }

        return ResponseEntity.status(HttpStatus.CREATED)
            .contentType(MediaType.APPLICATION_JSON)
            .body(response);
    }

    // ------------------------------------------------------------------
    // 写：更新（带冲突警告）
    // ------------------------------------------------------------------

    @PatchMapping("/{id}")
    public Map<String, Object> patch(@PathVariable String id,
                                     @Valid @RequestBody TaskPatchRequest req,
                                     @RequestHeader(value = "X-Actor", required = false) String actor,
                                     @RequestHeader(value = "X-Trace-Id", required = false) String traceId) {
        var result = taskService.patch(id, req, actor, traceId);
        return Map.of(
            "task", TaskResponse.from(result.task()),
            "warnings", result.warnings()
        );
    }

    // ------------------------------------------------------------------
    // 状态流转
    // ------------------------------------------------------------------

    @PostMapping("/{id}:complete")
    public TaskResponse complete(@PathVariable String id,
                                 @Valid @RequestBody CompleteRequest req,
                                 @RequestHeader(value = "X-Actor", required = false) String actor,
                                 @RequestHeader(value = "X-Trace-Id", required = false) String traceId) {
        return TaskResponse.from(taskService.complete(id, req.version(), actor, traceId));
    }

    @PostMapping("/{id}:start")
    public TaskResponse start(@PathVariable String id,
                              @Valid @RequestBody CompleteRequest req,
                              @RequestHeader(value = "X-Actor", required = false) String actor,
                              @RequestHeader(value = "X-Trace-Id", required = false) String traceId) {
        return TaskResponse.from(taskService.start(id, req.version(), actor, traceId));
    }

    @PostMapping("/{id}:reopen")
    public TaskResponse reopen(@PathVariable String id,
                               @Valid @RequestBody CompleteRequest req,
                               @RequestHeader(value = "X-Actor", required = false) String actor,
                               @RequestHeader(value = "X-Trace-Id", required = false) String traceId) {
        return TaskResponse.from(taskService.reopen(id, req.version(), actor, traceId));
    }

    @PostMapping("/{id}:cancel")
    public TaskResponse cancel(@PathVariable String id,
                               @Valid @RequestBody CompleteRequest req,
                               @RequestHeader(value = "X-Actor", required = false) String actor,
                               @RequestHeader(value = "X-Trace-Id", required = false) String traceId) {
        return TaskResponse.from(taskService.cancel(id, req.version(), actor, traceId));
    }

    // ------------------------------------------------------------------
    // 删除：两阶段确认
    // ------------------------------------------------------------------

    /**
     * 软删除。两阶段：
     *   第一次不带 X-Confirm-Token → 403 CONFIRM_REQUIRED，响应里带令牌和任务快照
     *   带上令牌再来 → 真正删除
     *
     * 为什么要这一步：删除是破坏性操作，且 Agent 是最容易误触的调用方。
     * 令牌 5 分钟过期、单次使用。
     */
    @DeleteMapping("/{id}")
    public ResponseEntity<Void> delete(@PathVariable String id,
                                       @RequestHeader(value = "X-Confirm-Token", required = false) String token,
                                       @RequestHeader(value = "X-Actor", required = false) String actor,
                                       @RequestHeader(value = "X-Trace-Id", required = false) String traceId) {
        var task = taskService.getOrThrow(id);

        if (!confirmTokens.consume(token, id)) {
            String newToken = confirmTokens.issue(id);
            throw new ConfirmRequiredException(
                "删除是破坏性操作，需要确认。请带上 X-Confirm-Token 重新请求。",
                newToken,
                TaskResponse.from(task));
        }

        taskService.softDelete(id, actor, traceId);
        return ResponseEntity.noContent().build();
    }

    // ------------------------------------------------------------------
    // 依赖
    // ------------------------------------------------------------------

    @PostMapping("/{id}/dependencies")
    @ResponseStatus(HttpStatus.CREATED)
    public List<String> addDependency(@PathVariable String id,
                                      @Valid @RequestBody DependencyRequest req) {
        dependencyService.addDependency(id, req.dependsOnId());
        return dependencyService.prerequisitesOf(id);
    }

    @DeleteMapping("/{id}/dependencies/{dependsOnId}")
    public ResponseEntity<Void> removeDependency(@PathVariable String id,
                                                 @PathVariable String dependsOnId) {
        dependencyService.removeDependency(id, dependsOnId);
        return ResponseEntity.noContent().build();
    }

    @GetMapping("/{id}/dependencies")
    public List<String> dependencies(@PathVariable String id) {
        return dependencyService.prerequisitesOf(id);
    }

    // ------------------------------------------------------------------
    // 审计查询
    // ------------------------------------------------------------------

    @GetMapping("/{id}/history")
    public List<Map<String, Object>> history(@PathVariable String id) {
        return auditService.history(id).stream()
            .map(a -> {
                Map<String, Object> m = new java.util.LinkedHashMap<>();
                m.put("action", a.getAction());
                m.put("actor", a.getActor());
                m.put("traceId", a.getTraceId());
                m.put("before", a.getBeforeJson());
                m.put("after", a.getAfterJson());
                m.put("at", a.getCreatedAt().toString());
                return m;
            })
            .toList();
    }
}
