package io.cadence.core;

import org.junit.jupiter.api.Test;

/**
 * 上下文冒烟测试。
 *
 * 直接继承 AbstractIntegrationTest 而不是单独写 @SpringBootTest：
 * 后者没有 DynamicPropertySource，会去连 application.properties 里的
 * localhost:5433，一旦本地 docker compose 没起就报"连接被拒绝"。
 * 继承后共用同一个 Testcontainers Postgres，与其余集成测试行为一致。
 */
class CadenceCoreApplicationTests extends AbstractIntegrationTest {

	@Test
	void contextLoads() {
	}

}
