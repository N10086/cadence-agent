package io.cadence.core.repository;

import io.cadence.core.domain.AuditLog;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface AuditLogRepository extends JpaRepository<AuditLog, Long> {

    List<AuditLog> findByTaskIdOrderByCreatedAtDesc(String taskId);

    List<AuditLog> findByTraceIdOrderByCreatedAtAsc(String traceId);

    List<AuditLog> findTop100ByOrderByCreatedAtDesc();
}
