package io.cadence.core;

import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

/**
 * 集成测试基类：跑真 Postgres，不用 H2。
 *
 * 单例容器：所有子类共享同一个容器实例，
 * 否则每个测试类都要拉一次镜像、跑一次 Flyway，整个测试套件会慢十倍。
 * Java 的 static 字段在这里正好承担"进程级单例"的角色。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
public abstract class AbstractIntegrationTest {

    @SuppressWarnings("resource")
    protected static final PostgreSQLContainer<?> POSTGRES =
        new PostgreSQLContainer<>(DockerImageName.parse("postgres:16-alpine"))
            .withDatabaseName("cadence_test")
            .withUsername("cadence")
            .withPassword("cadence")
            .withInitScript(null);   // schema 交给 Flyway

    static {
        POSTGRES.start();
    }

    @DynamicPropertySource
    static void datasource(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", POSTGRES::getJdbcUrl);
        registry.add("spring.datasource.username", POSTGRES::getUsername);
        registry.add("spring.datasource.password", POSTGRES::getPassword);
    }
}
