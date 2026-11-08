package io.cadence.core.domain;

import jakarta.persistence.Column;
import jakarta.persistence.EmbeddedId;
import jakarta.persistence.Entity;
import jakarta.persistence.Table;
import java.io.Serializable;
import java.time.OffsetDateTime;
import java.util.Objects;

/**
 * 任务依赖：task_id 依赖 depends_on_id（FINISH_TO_START）。
 *
 * 语义：depends_on_id（前置）未完成时，task_id（后继）不允许进入 DONE。
 *
 * 用复合主键 (task_id, depends_on_id) 而不是自增 id，
 * 因为"同一对依赖只能存在一次"是业务约束，让数据库兜住它。
 */
@Entity
@Table(name = "task_dependency")
public class TaskDependency {

    @EmbeddedId
    private Key id;

    @Column(name = "created_at", nullable = false, updatable = false)
    private OffsetDateTime createdAt;

    protected TaskDependency() {}

    public TaskDependency(String taskId, String dependsOnId) {
        this.id = new Key(taskId, dependsOnId);
        this.createdAt = OffsetDateTime.now();
    }

    public String getTaskId() { return id.getTaskId(); }
    public String getDependsOnId() { return id.getDependsOnId(); }
    public OffsetDateTime getCreatedAt() { return createdAt; }

    public static class Key implements Serializable {
        @Column(name = "task_id", length = 36, nullable = false)
        private String taskId;

        @Column(name = "depends_on_id", length = 36, nullable = false)
        private String dependsOnId;

        protected Key() {}

        public Key(String taskId, String dependsOnId) {
            this.taskId = taskId;
            this.dependsOnId = dependsOnId;
        }

        public String getTaskId() { return taskId; }
        public String getDependsOnId() { return dependsOnId; }

        @Override
        public boolean equals(Object o) {
            if (this == o) return true;
            if (!(o instanceof Key k)) return false;
            return Objects.equals(taskId, k.taskId) && Objects.equals(dependsOnId, k.dependsOnId);
        }

        @Override
        public int hashCode() {
            return Objects.hash(taskId, dependsOnId);
        }
    }
}
