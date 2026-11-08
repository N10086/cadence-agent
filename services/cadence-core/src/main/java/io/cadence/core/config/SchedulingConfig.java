package io.cadence.core.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.annotation.EnableScheduling;

/** 启用定时任务：幂等记录清理、冲突重扫等。 */
@Configuration
@EnableScheduling
public class SchedulingConfig {
}
