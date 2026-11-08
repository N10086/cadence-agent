package io.cadence.core.service;

import io.cadence.core.domain.IdempotencyRecord;
import io.cadence.core.repository.IdempotencyRecordRepository;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.Optional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 幂等。
 *
 * 解决的问题：客户端（尤其是 Agent）发出创建请求后网络超时，
 * 重试时服务端其实已经建好了 —— 结果就是两个一模一样的任务。
 *
 * 做法：请求带 X-Idempotency-Key，首次执行成功后把响应存起来；
 * 同键再来的请求直接返回存着的响应，不执行业务逻辑。
 *
 * 24 小时 TTL。为什么不永久保存：
 *   - 幂等键是客户端生成的，时间窗口只需覆盖"重试可能发生"的区间
 *   - 永久保存会让这张表无限增长，且每次创建都要查它
 */
@Service
public class IdempotencyService {

    private static final Logger log = LoggerFactory.getLogger(IdempotencyService.class);
    private static final Duration TTL = Duration.ofHours(24);

    private final IdempotencyRecordRepository repo;

    public IdempotencyService(IdempotencyRecordRepository repo) {
        this.repo = repo;
    }

    @Transactional(readOnly = true)
    public Optional<IdempotencyRecord> find(String key) {
        if (key == null || key.isBlank()) {
            return Optional.empty();
        }
        return repo.findById(key)
            .filter(r -> r.getCreatedAt().isAfter(OffsetDateTime.now().minus(TTL)));
    }

    @Transactional
    public void save(String key, String taskId, String responseJson) {
        if (key == null || key.isBlank()) {
            return;
        }
        repo.save(new IdempotencyRecord(key, taskId, responseJson));
    }

    /** 每 6 小时清理一次过期记录。不清理的话这张表只增不减。 */
    @Scheduled(initialDelay = 60_000, fixedDelay = 6 * 60 * 60 * 1000)
    @Transactional
    public void cleanup() {
        int removed = repo.deleteOlderThan(OffsetDateTime.now().minus(TTL));
        if (removed > 0) {
            log.info("清理了 {} 条过期幂等记录", removed);
        }
    }
}
