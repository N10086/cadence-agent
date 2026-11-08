package io.cadence.core.service;

import java.security.SecureRandom;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.stereotype.Service;

/**
 * 确认令牌。
 *
 * 流程（两阶段）：
 *   1. DELETE 不带 X-Confirm-Token → 403 CONFIRM_REQUIRED，响应里带 token 和任务快照
 *   2. 带上该 token 再来 → 真正删除
 *
 * 令牌本身用 SecureRandom 生成，不用可猜测的序号。
 * 5 分钟过期、单次使用 —— 用掉即失效，防止重放。
 *
 * 单机内存实现。多实例部署时需要换成共享存储，
 * 但那是 Non-Goal（见 PRD），真要做也从 Redis 起步。
 */
@Service
public class ConfirmTokenService {

    private static final Duration TTL = Duration.ofMinutes(5);
    private static final SecureRandom RNG = new SecureRandom();

    private record Entry(String resourceId, OffsetDateTime expiresAt) {}

    private final Map<String, Entry> tokens = new ConcurrentHashMap<>();

    /** 为某个资源签发一个令牌 */
    public String issue(String resourceId) {
        byte[] buf = new byte[16];
        RNG.nextBytes(buf);
        String token = "ct_" + HexFormat.of().formatHex(buf);
        tokens.put(token, new Entry(resourceId, OffsetDateTime.now().plus(TTL)));
        sweep();
        return token;
    }

    /**
     * 校验并消费令牌。
     * 返回 true 表示校验通过且已失效（单次使用）。
     */
    public boolean consume(String token, String resourceId) {
        if (token == null || token.isBlank()) {
            return false;
        }
        Entry e = tokens.remove(token);   // 取出即消费，无论后续校验是否通过
        if (e == null) {
            return false;
        }
        if (e.expiresAt().isBefore(OffsetDateTime.now())) {
            return false;
        }
        return e.resourceId().equals(resourceId);
    }

    private void sweep() {
        OffsetDateTime now = OffsetDateTime.now();
        tokens.entrySet().removeIf(en -> en.getValue().expiresAt().isBefore(now));
    }
}
