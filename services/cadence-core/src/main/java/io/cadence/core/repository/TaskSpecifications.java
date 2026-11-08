package io.cadence.core.repository;

import io.cadence.core.domain.Priority;
import io.cadence.core.domain.Task;
import io.cadence.core.domain.TaskStatus;
import jakarta.persistence.criteria.Expression;
import jakarta.persistence.criteria.Predicate;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import org.springframework.data.jpa.domain.Specification;

/**
 * 任务的动态查询条件。
 *
 * 约定：所有参数为 null 即"该条件不生效"。
 * 已软删除的任务永不返回 —— 这条写死在最前面，任何调用方都无法绕过。
 */
public final class TaskSpecifications {

    private TaskSpecifications() {}

    public static Specification<Task> byFilter(
            String id,
            String keyword,
            TaskStatus status,
            Priority priority,
            OffsetDateTime dueBefore,
            OffsetDateTime dueAfter,
            String tag) {

        return (root, query, cb) -> {
            List<Predicate> ps = new ArrayList<>();

            // 硬规则：永不返回已删除
            ps.add(cb.isNull(root.get("deletedAt")));

            if (id != null) {
                ps.add(cb.equal(root.get("id"), id));
            }
            if (keyword != null && !keyword.isBlank()) {
                String like = "%" + keyword.trim().toLowerCase() + "%";
                Expression<String> lowerTitle = cb.lower(root.get("title"));
                ps.add(cb.like(lowerTitle, like));
            }
            if (status != null) {
                ps.add(cb.equal(root.get("status"), status));
            }
            if (priority != null) {
                ps.add(cb.equal(root.get("priority"), priority));
            }
            if (dueBefore != null) {
                ps.add(cb.lessThanOrEqualTo(root.get("dueAt"), dueBefore));
            }
            if (dueAfter != null) {
                ps.add(cb.greaterThanOrEqualTo(root.get("dueAt"), dueAfter));
            }

            return cb.and(ps.toArray(Predicate[]::new));
        };
    }

    /**
     * 标签过滤需要单独的判定：在 Criteria 里对 text[] 做包含。
     *
     * 这里用表达式函数 `array_position(tags, :tag) IS NOT NULL`，
     * 而不是 JPA 的 `MEMBER OF`。前者是 PostgreSQL 原生语义，不受 JPA 集合映射的影响。
     */
    public static Specification<Task> hasTag(String tag) {
        return (root, query, cb) -> {
            if (tag == null || tag.isBlank()) {
                return cb.conjunction();
            }
            Expression<Integer> pos = cb.function(
                "array_position",
                Integer.class,
                root.get("tags"),
                cb.literal(tag.trim()));
            return cb.isNotNull(pos);
        };
    }
}
