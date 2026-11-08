package io.cadence.core.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.PrePersist;
import jakarta.persistence.PreUpdate;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

/**
 * 任务的领域实体。
 *
 * 三个刻意的设计：
 * 1. id 用 UUID 字符串，不用自增主键 —— 自增 id 会让模型倾向于"猜" 1、2、3，幻觉率飙升
 * 2. 时间一律 OffsetDateTime + timestamptz —— "下周三"的语义必须在存储层保留
 * 3. version 乐观锁，且必须出现在响应里 —— Agent 的 task.update 依赖它，拿不到就会编造
 */
@Entity
@Table(name = "task")
public class Task {

    @Id
    @Column(name = "id", nullable = false, updatable = false, length = 36)
    private String id;

    @Column(name = "title", nullable = false, length = 200)
    private String title;

    @Column(name = "description", columnDefinition = "text")
    private String description;

    @Enumerated(EnumType.STRING)
    @Column(name = "status", nullable = false, length = 16)
    private TaskStatus status = TaskStatus.TODO;

    @Enumerated(EnumType.STRING)
    @Column(name = "priority", nullable = false, length = 16)
    private Priority priority = Priority.NORMAL;

    @Column(name = "start_at")
    private OffsetDateTime startAt;

    @Column(name = "due_at")
    private OffsetDateTime dueAt;

    /** 预估耗时（分钟），5-480。求解器的核心输入之一。 */
    @Column(name = "estimate_min")
    private Integer estimateMin;

    @Column(name = "parent_id", length = 36)
    private String parentId;

    /** 重排期时"不能挪"的标记。 */
    @Column(name = "pinned", nullable = false)
    private boolean pinned = false;

    /** iCal RRULE 子集，如 FREQ=WEEKLY;BYDAY=MO */
    @Column(name = "rrule", length = 200)
    private String rrule;

    @JdbcTypeCode(SqlTypes.ARRAY)
    @Column(name = "tags", columnDefinition = "text[]")
    private List<String> tags = new ArrayList<>();

    @Version
    @Column(name = "version", nullable = false)
    private Long version;

    @Column(name = "deleted_at")
    private OffsetDateTime deletedAt;

    @Column(name = "created_at", nullable = false, updatable = false)
    private OffsetDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private OffsetDateTime updatedAt;

    @PrePersist
    void onCreate() {
        OffsetDateTime now = OffsetDateTime.now();
        if (id == null) {
            id = UUID.randomUUID().toString();
        }
        createdAt = now;
        updatedAt = now;
    }

    @PreUpdate
    void onUpdate() {
        updatedAt = OffsetDateTime.now();
    }

    public boolean isDeleted() {
        return deletedAt != null;
    }

    // ---------- getters / setters ----------

    public String getId() { return id; }
    public void setId(String id) { this.id = id; }

    public String getTitle() { return title; }
    public void setTitle(String title) { this.title = title; }

    public String getDescription() { return description; }
    public void setDescription(String description) { this.description = description; }

    public TaskStatus getStatus() { return status; }
    public void setStatus(TaskStatus status) { this.status = status; }

    public Priority getPriority() { return priority; }
    public void setPriority(Priority priority) { this.priority = priority; }

    public OffsetDateTime getStartAt() { return startAt; }
    public void setStartAt(OffsetDateTime startAt) { this.startAt = startAt; }

    public OffsetDateTime getDueAt() { return dueAt; }
    public void setDueAt(OffsetDateTime dueAt) { this.dueAt = dueAt; }

    public Integer getEstimateMin() { return estimateMin; }
    public void setEstimateMin(Integer estimateMin) { this.estimateMin = estimateMin; }

    public String getParentId() { return parentId; }
    public void setParentId(String parentId) { this.parentId = parentId; }

    public boolean isPinned() { return pinned; }
    public void setPinned(boolean pinned) { this.pinned = pinned; }

    public String getRrule() { return rrule; }
    public void setRrule(String rrule) { this.rrule = rrule; }

    public List<String> getTags() { return tags; }
    public void setTags(List<String> tags) { this.tags = tags == null ? new ArrayList<>() : tags; }

    public Long getVersion() { return version; }

    public OffsetDateTime getDeletedAt() { return deletedAt; }
    public void setDeletedAt(OffsetDateTime deletedAt) { this.deletedAt = deletedAt; }

    public OffsetDateTime getCreatedAt() { return createdAt; }
    public OffsetDateTime getUpdatedAt() { return updatedAt; }
}
