package io.cadence.core.service;

import io.cadence.core.domain.Priority;
import io.cadence.core.domain.Task;
import io.cadence.core.domain.TaskStatus;
import io.cadence.core.domain.TimeWindow;
import io.cadence.core.repository.TaskRepository;
import io.cadence.core.repository.TaskSpecifications;
import io.cadence.core.web.dto.TaskCreateRequest;
import io.cadence.core.web.dto.TaskPatchRequest;
import io.cadence.core.web.dto.Warning;
import io.cadence.core.web.error.VersionConflictException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import org.springframework.data.domain.Sort;
import org.springframework.data.jpa.domain.Specification;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class TaskService {

    private static final Sort DEFAULT_SORT = Sort.by(
        Sort.Order.asc("dueAt"),
        Sort.Order.asc("createdAt")
    );

    private final TaskRepository repo;
    private final DependencyService dependencyService;
    private final ConflictService conflictService;
    private final AuditService audit;
    private final TaskStateMachine stateMachine;
    private final IdempotencyService idempotency;

    public TaskService(TaskRepository repo,
                       DependencyService dependencyService,
                       ConflictService conflictService,
                       AuditService audit,
                       TaskStateMachine stateMachine,
                       IdempotencyService idempotency) {
        this.repo = repo;
        this.dependencyService = dependencyService;
        this.conflictService = conflictService;
        this.audit = audit;
        this.stateMachine = stateMachine;
        this.idempotency = idempotency;
    }

    // ==================================================================
    // 查询
    // ==================================================================

    @Transactional(readOnly = true)
    public List<Task> search(String id, String keyword, TaskStatus status, Priority priority,
                             OffsetDateTime dueBefore, OffsetDateTime dueAfter, String tag) {

        Specification<Task> spec = TaskSpecifications.byFilter(
                id, keyword, status, priority, dueBefore, dueAfter, tag)
            .and(TaskSpecifications.hasTag(tag));

        return repo.findAll(spec, DEFAULT_SORT);
    }

    @Transactional(readOnly = true)
    public Task getOrThrow(String id) {
        return liveTask(id);
    }

    // ==================================================================
    // 创建（带幂等）
    // ==================================================================

    @Transactional
    public Task create(TaskCreateRequest req, String actor, String traceId) {
        Task t = new Task();
        t.setTitle(req.title().trim());
        t.setDescription(req.description());
        t.setPriority(req.priority() == null ? Priority.NORMAL : req.priority());
        t.setStartAt(req.startAt());
        t.setDueAt(req.dueAt());
        t.setEstimateMin(req.estimateMin());
        t.setParentId(req.parentId());
        t.setPinned(Boolean.TRUE.equals(req.pinned()));
        t.setRrule(req.rrule());
        t.setTags(req.tags());
        t.setStatus(TaskStatus.TODO);

        Task saved = repo.save(t);
        audit.record(saved, AuditService.Action.CREATE, actor, traceId, null, audit.toJson(saved));
        return saved;
    }

    /** 兼容旧签名 */
    @Transactional
    public Task create(TaskCreateRequest req) {
        return create(req, "anonymous", null);
    }

    // ==================================================================
    // 更新（带冲突警告）
    // ==================================================================

    /**
     * 局部更新。
     *
     * @return 更新后的任务 + 冲突警告（不阻止写入，只提示）
     */
    @Transactional
    public UpdateResult patch(String id, TaskPatchRequest req, String actor, String traceId) {
        Task t = liveTask(id);
        assertVersionMatches(t, req.version());

        String before = audit.toJson(t);

        if (req.title() != null)       t.setTitle(req.title().trim());
        if (req.description() != null) t.setDescription(req.description());
        if (req.priority() != null)    t.setPriority(req.priority());
        if (req.startAt() != null)     t.setStartAt(req.startAt());
        if (req.dueAt() != null)       t.setDueAt(req.dueAt());
        if (req.estimateMin() != null) t.setEstimateMin(req.estimateMin());
        if (req.pinned() != null)      t.setPinned(req.pinned());
        if (req.rrule() != null)       t.setRrule(req.rrule());
        if (req.tags() != null)        t.setTags(req.tags());

        Task saved = repo.save(t);
        audit.record(saved, AuditService.Action.UPDATE, actor, traceId, before, audit.toJson(saved));

        List<Warning> warnings = conflictWarnings(saved);
        return new UpdateResult(saved, warnings);
    }

    @Transactional
    public Task patch(String id, TaskPatchRequest req) {
        return patch(id, req, "anonymous", null).task();
    }

    /**
     * 计算某个任务的时间冲突警告。
     *
     * FR-8 明确：冲突是警告而非错误，必须返回 200 + warnings[]。
     * 因为用户可能确实想把两个任务放在同一时段，
     * 阻止写入会让 Agent 陷入无意义的纠错循环。
     */
    public List<Warning> conflictWarnings(Task t) {
        var conflicts = conflictService.detect(t.getId(), t.getStartAt(), t.getDueAt(), t.getEstimateMin());
        List<Warning> out = new ArrayList<>();
        for (var c : conflicts) {
            out.add(new Warning(
                "TIME_CONFLICT",
                "与任务「" + c.title() + "」的时段重叠 " + c.overlapMinutes() + " 分钟",
                c.taskId()));
        }
        return out;
    }

    // ==================================================================
    // 状态流转（全部走状态机 + 审计）
    // ==================================================================

    @Transactional
    public Task complete(String id, Long version, String actor, String traceId) {
        Task t = liveTask(id);

        if (t.getStatus() == TaskStatus.DONE) {
            return t;   // 幂等
        }
        stateMachine.assertCanComplete(t.getStatus());
        assertVersionMatches(t, version);

        List<String> unfinished = dependencyService.unfinishedPrerequisites(id);
        if (!unfinished.isEmpty()) {
            throw new io.cadence.core.web.error.DependencyViolationException(
                "任务「" + t.getTitle() + "」的前置任务尚未完成: " + String.join(", ", unfinished));
        }

        String before = audit.toJson(t);
        t.setStatus(TaskStatus.DONE);
        Task saved = repo.save(t);
        audit.record(saved, AuditService.Action.COMPLETE, actor, traceId, before, audit.toJson(saved));
        return saved;
    }

    @Transactional
    public Task complete(String id, Long version) {
        return complete(id, version, "anonymous", null);
    }

    @Transactional
    public Task start(String id, Long version, String actor, String traceId) {
        Task t = liveTask(id);
        if (t.getStatus() == TaskStatus.DOING) {
            return t;   // 幂等
        }
        stateMachine.assertCanStart(t.getStatus());
        assertVersionMatches(t, version);

        String before = audit.toJson(t);
        t.setStatus(TaskStatus.DOING);
        Task saved = repo.save(t);
        audit.record(saved, AuditService.Action.START, actor, traceId, before, audit.toJson(saved));
        return saved;
    }

    @Transactional
    public Task reopen(String id, Long version, String actor, String traceId) {
        Task t = liveTask(id);
        if (t.getStatus() == TaskStatus.TODO) {
            return t;   // 幂等
        }
        stateMachine.assertCanReopen(t.getStatus());
        assertVersionMatches(t, version);

        String before = audit.toJson(t);
        t.setStatus(TaskStatus.TODO);
        Task saved = repo.save(t);
        audit.record(saved, AuditService.Action.REOPEN, actor, traceId, before, audit.toJson(saved));
        return saved;
    }

    @Transactional
    public Task cancel(String id, Long version, String actor, String traceId) {
        Task t = liveTask(id);
        if (t.getStatus() == TaskStatus.CANCELLED) {
            return t;   // 幂等
        }
        stateMachine.assertCanCancel(t.getStatus());

        String before = audit.toJson(t);
        t.setStatus(TaskStatus.CANCELLED);
        Task saved = repo.save(t);
        audit.record(saved, AuditService.Action.CANCEL, actor, traceId, before, audit.toJson(saved));
        return saved;
    }

    // ==================================================================
    // 删除（两阶段确认）
    // ==================================================================

    @Transactional
    public void softDelete(String id, String actor, String traceId) {
        Task t = liveTask(id);

        String before = audit.toJson(t);
        t.setDeletedAt(OffsetDateTime.now());
        Task saved = repo.save(t);
        audit.record(saved, AuditService.Action.DELETE, actor, traceId, before, null);
    }

    @Transactional
    public void softDelete(String id) {
        softDelete(id, "anonymous", null);
    }

    // ==================================================================
    // helpers
    // ==================================================================

    private Task liveTask(String id) {
        Task t = repo.findById(id)
            .orElseThrow(() -> new TaskNotFoundException(id));
        if (t.isDeleted()) {
            throw new TaskNotFoundException(id);
        }
        return t;
    }

    private void assertVersionMatches(Task t, Long expected) {
        if (expected != null && !expected.equals(t.getVersion())) {
            throw new VersionConflictException(t.getId(), expected, t.getVersion());
        }
    }

    public record UpdateResult(Task task, List<Warning> warnings) {}
}
