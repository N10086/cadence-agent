package io.cadence.core.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.OffsetDateTime;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

/**
 * 幂等记录：一次带 X-Idempotency-Key 的创建请求的完整响应。
 *
 * 重复请求直接返回这里存着的 response，不再执行创建逻辑。
 * 这解决的是"网络超时后客户端重试，结果建了两个同样的任务"。
 */
@Entity
@Table(name = "idempotency_record")
public class IdempotencyRecord {

    @Id
    @Column(name = "idem_key", length = 128)
    private String idemKey;

    @Column(name = "task_id", nullable = false, length = 36)
    private String taskId;

    @JdbcTypeCode(SqlTypes.JSON)
    @Column(name = "response", nullable = false, columnDefinition = "jsonb")
    private String response;

    @Column(name = "created_at", nullable = false, updatable = false)
    private OffsetDateTime createdAt;

    protected IdempotencyRecord() {}

    public IdempotencyRecord(String idemKey, String taskId, String response) {
        this.idemKey = idemKey;
        this.taskId = taskId;
        this.response = response;
        this.createdAt = OffsetDateTime.now();
    }

    public String getIdemKey() { return idemKey; }
    public String getTaskId() { return taskId; }
    public String getResponse() { return response; }
    public OffsetDateTime getCreatedAt() { return createdAt; }
}
