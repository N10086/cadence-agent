package io.cadence.core.repository;

import io.cadence.core.domain.IdempotencyRecord;
import java.time.OffsetDateTime;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface IdempotencyRecordRepository extends JpaRepository<IdempotencyRecord, String> {

    /**
     * 清理过期记录。
     * 不清理的话这张表只增不减，且每次创建请求都要先查它，索引会越来越大。
     * 由 IdempotencyCleanupJob 定时调用。
     */
    @Modifying
    @Query("DELETE FROM IdempotencyRecord r WHERE r.createdAt < :cutoff")
    int deleteOlderThan(@Param("cutoff") OffsetDateTime cutoff);
}
