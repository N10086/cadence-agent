package io.cadence.core.service;

import io.cadence.core.domain.AuditLog;
import io.cadence.core.domain.Task;
import io.cadence.core.repository.AuditLogRepository;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 审计。
 *
 * 原则：审计写入与业务写入在同一个事务里。
 * 业务回滚 → 审计也回滚，不会出现"记录了改动但实际没改"的假账。
 *
 * 反过来的代价是新事务挂了会拖垮主流程 —— 但这个是有意选择的：
 * 对任务系统来说，留下了无法解释的痕迹比写失败更糟。
 */
@Service
public class AuditService {

    private final AuditLogRepository repo;
    private final tools.jackson.databind.ObjectMapper json;

    public AuditService(AuditLogRepository repo) {
        this.repo = repo;
        this.json = new tools.jackson.databind.ObjectMapper();
    }

    public enum Action {
        CREATE, UPDATE, COMPLETE, START, REOPEN, CANCEL, DELETE, BATCH_UPDATE
    }

    /** 记录一次写操作。before 为 null 表示新建，after 为 null 表示删除。 */
    @Transactional
    public void record(Task task, Action action, String actor, String traceId,
                       String beforeJson, String afterJson) {
        String safeActor = (actor == null || actor.isBlank()) ? "anonymous" : actor;
        repo.save(new AuditLog(
            task.getId(), action.name(), safeActor, traceId, beforeJson, afterJson));
    }

    public String toJson(Task task) {
        if (task == null) return null;
        try {
            return json.writeValueAsString(io.cadence.core.web.dto.TaskResponse.from(task));
        } catch (Exception e) {
            return null;
        }
    }

    @Transactional(readOnly = true)
    public List<AuditLog> history(String taskId) {
        return repo.findByTaskIdOrderByCreatedAtDesc(taskId);
    }

    @Transactional(readOnly = true)
    public List<AuditLog> byTrace(String traceId) {
        return repo.findByTraceIdOrderByCreatedAtAsc(traceId);
    }
}
