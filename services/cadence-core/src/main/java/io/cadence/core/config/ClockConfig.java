package io.cadence.core.config;

import java.time.Clock;
import java.time.ZoneId;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * 时间必须可注入。
 * 直接调 OffsetDateTime.now() 的地方，测试就无法冻结时间，
 * 而"下周三到底是哪天"是 40 条用例的根基。
 */
@Configuration
public class ClockConfig {

    public static final ZoneId APP_ZONE = ZoneId.of("Asia/Shanghai");

    @Bean
    public Clock clock() {
        return Clock.system(APP_ZONE);
    }
}
