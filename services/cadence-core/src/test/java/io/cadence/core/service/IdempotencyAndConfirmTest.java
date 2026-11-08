package io.cadence.core.service;

import static org.assertj.core.api.Assertions.assertThat;

import io.cadence.core.AbstractIntegrationTest;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;

/**
 * FR-10 幂等 + FR-4 确认令牌。
 *
 * 两个都不是"业务功能"，而是**给不可靠网络与不可靠调用方兜底**的机制，
 * 恰恰是 Agent 接入后最需要的东西。
 */
class IdempotencyAndConfirmTest extends AbstractIntegrationTest {

    @Autowired IdempotencyService idem;
    @Autowired ConfirmTokenService confirm;
    @Autowired JdbcTemplate jdbc;

    @BeforeEach
    void clean() {
        jdbc.execute("TRUNCATE audit_log, idempotency_record, task_dependency, task CASCADE");
    }

    // ==================================================================
    @Test
    @DisplayName("幂等：存了之后再查能拿到原响应")
    void 幂等存取() {
        idem.save("key-1", "t_001", "{\"id\":\"t_001\"}");

        var found = idem.find("key-1");
        assertThat(found).isPresent();
        assertThat(found.get().getTaskId()).isEqualTo("t_001");
        assertThat(found.get().getResponse()).contains("t_001");
    }

    @Test
    @DisplayName("幂等：不存在的键返回空")
    void 幂等未命中() {
        assertThat(idem.find("nope")).isEmpty();
    }

    @Test
    @DisplayName("幂等：空键不记录也不查询")
    void 幂等空键() {
        idem.save(null, "t_001", "{}");
        idem.save("", "t_001", "{}");
        idem.save("   ", "t_001", "{}");

        assertThat(idem.find(null)).isEmpty();
        assertThat(idem.find("")).isEmpty();
    }

    @Test
    @DisplayName("幂等：过期记录不被命中")
    void 幂等过期() {
        idem.save("key-old", "t_001", "{}");
        // 手工把时间改成 25 小时前
        jdbc.update("UPDATE idempotency_record SET created_at = now() - interval '25 hours' WHERE idem_key = 'key-old'");

        assertThat(idem.find("key-old")).isEmpty();
    }

    @Test
    @DisplayName("幂等：清理只删过期记录")
    void 幂等清理() {
        idem.save("fresh", "t_001", "{}");
        idem.save("stale", "t_002", "{}");
        jdbc.update("UPDATE idempotency_record SET created_at = now() - interval '25 hours' WHERE idem_key = 'stale'");

        idem.cleanup();

        assertThat(idem.find("fresh")).isPresent();
        assertThat(jdbc.queryForObject(
            "SELECT count(*) FROM idempotency_record WHERE idem_key = 'stale'", Integer.class)).isZero();
    }

    // ==================================================================
    @Test
    @DisplayName("确认令牌：正确令牌可使用一次")
    void 令牌单次使用() {
        String token = confirm.issue("t_001");

        assertThat(confirm.consume(token, "t_001")).isTrue();
        // 第二次用同一个令牌失败
        assertThat(confirm.consume(token, "t_001")).isFalse();
    }

    @Test
    @DisplayName("确认令牌：资源不匹配不通过")
    void 令牌绑定资源() {
        String token = confirm.issue("t_001");
        assertThat(confirm.consume(token, "t_999")).isFalse();
    }

    @Test
    @DisplayName("确认令牌：空令牌不通过")
    void 令牌为空() {
        assertThat(confirm.consume(null, "t_001")).isFalse();
        assertThat(confirm.consume("", "t_001")).isFalse();
    }

    @Test
    @DisplayName("确认令牌：伪造令牌不通过")
    void 伪造令牌() {
        assertThat(confirm.consume("ct_deadbeef", "t_001")).isFalse();
    }

    @Test
    @DisplayName("确认令牌：每次签发都不一样")
    void 令牌唯一() {
        var a = confirm.issue("t_001");
        var b = confirm.issue("t_001");
        assertThat(a).isNotEqualTo(b);
        assertThat(a).startsWith("ct_");
        assertThat(a.length()).isGreaterThan(20);   // 16 字节 hex
    }
}
