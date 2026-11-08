package io.cadence.core.repository;

import io.cadence.core.domain.TaskDependency;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface TaskDependencyRepository extends JpaRepository<TaskDependency, TaskDependency.Key> {

    /** 我依赖谁（我的前置） */
    List<TaskDependency> findByIdTaskId(String taskId);

    /** 谁依赖我（我的后继） */
    List<TaskDependency> findByIdDependsOnId(String dependsOnId);

    List<TaskDependency> findAllByOrderByIdTaskIdAscIdDependsOnIdAsc();
}
