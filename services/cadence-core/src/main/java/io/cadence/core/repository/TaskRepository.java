package io.cadence.core.repository;

import io.cadence.core.domain.Priority;
import io.cadence.core.domain.Task;
import io.cadence.core.domain.TaskStatus;
import java.time.OffsetDateTime;
import java.util.Collection;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.JpaSpecificationExecutor;

/**
 * 用 Specification 做动态条件，而不是手写 JPQL。
 *
 * 原因：手写 JPQL 里的 `:tag MEMBER OF t.tags` 在 Hibernate 6.6 + text[] 映射下
 * 会抛 "Operand of 'member of' operator must be a plural path" —— 集合的 JPA 映射语义
 * 与 PostgreSQL 原生数组之间不总是对齐。Specification 生成的 Criteria 查询走的是
 * 同一套映射，但用 `t.tags = :tag` 的数组包含判定（见 TaskSpecifications），
 * 不会触发这个语义检查。
 *
 * 派生查询保留给最简单的场景（按 id 查、按状态查），复杂组合走 Specification。
 */
public interface TaskRepository extends JpaRepository<Task, String>, JpaSpecificationExecutor<Task> {

    boolean existsByIdAndDeletedAtIsNull(String id);

    List<Task> findByParentIdAndDeletedAtIsNull(String parentId);

    List<Task> findByStatusAndDeletedAtIsNull(TaskStatus status);
}
