package io.cadence.core.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.OffsetDateTime;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

/**
 * 审计日志：每一次写操作的完整留痕。
 *
 * 之所以存 before/after 的 JSON 快照而不是 diff：
 * diff 需要额外的计算和存储格式，而快照可以直接对比、直接回放。
 * 这个表只追加、不更新，所以存冗余完全可接受。
 */
@Entity
@Table(name = "audit_log")
public class AuditLog {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "task_id", nullable = false, length = 36)
    private String taskId;

    @Column(name = "action", nullable = false, length = 32)
    private String action;

    @Column(name = "actor", nullable = false, length = 64)
    private String actor;

    @Column(name = "trace_id", length = 64)
    private String traceId;

    @JdbcTypeCode(SqlTypes.JSON)
    @Column(name = "before_json", columnDefinition = "jsonb")
    private String beforeJson;

    @JdbcTypeCode(SqlTypes.JSON)
    @Column(name = "after_json", columnDefinition = "jsonb")
    private String afterJson;

    @Column(name = "created_at", nullable = false, updatable = false)
    private OffsetDateTime createdAt;

    protected AuditLog() {}

    public AuditLog(String taskId, String action, String actor, String traceId,
                    String beforeJson, String afterJson) {
        this.taskId = taskId;
        this.action = action;
        this.actor = actor;
        this.traceId = traceId;
        this.beforeJson = beforeJson;
        this.afterJson = afterJson;
        this.createdAt = OffsetDateTime.now();
    }

    public Long getId() { return id; }
    public String getTaskId() { return taskId; }
    public String getAction() { return action; }
    public String getActor() { return actor; }
    public String getTraceId() { return traceId; }
    public String getBeforeJson() { return beforeJson; }
    public String getAfterJson() { return afterJson; }
    public OffsetDateTime getCreatedAt() { return createdAt; }
}
