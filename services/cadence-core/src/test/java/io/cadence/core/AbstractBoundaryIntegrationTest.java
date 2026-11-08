package io.cadence.core;

import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.utility.DockerImageName;

/**
 * 边界测试专用集成测试基类。
 *
 * 与 {@link AbstractIntegrationTest} 的区别只有一个：**允许使用外部数据库**。
 *
 * - CI / 正常开发：不设 {@code CORE_TEST_DB_URL}，自动拉 Testcontainers，
 *   行为与现有 IT 完全一致。
 * - 受限环境（Docker 守护进程不可访问、但本机已有 PG 5433）：
 *   设 {@code CORE_TEST_DB_URL=jdbc:postgresql://localhost:5433/cadence_boundary_test}，
 *   跳过 Docker，直接跑同一个真 PostgreSQL 16。测试库由运行者事先建好，
 *   本类只负责每次测试前清表（见各测试类的 {@code @BeforeEach}）。
 *
 * 为什么不在生产测试基类上改：现有 IT 的容器语义是"测试环境 = 生产环境"，
 * 边界测试不应该改变这个前提；这里只是给受限环境留一个可执行的入口。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
public abstract class AbstractBoundaryIntegrationTest {

    private static final String EXTERNAL_URL = System.getenv("CORE_TEST_DB_URL");
    private static final String EXTERNAL_USER = System.getenv().getOrDefault("CORE_TEST_DB_USER", "cadence");
    private static final String EXTERNAL_PASSWORD = System.getenv().getOrDefault("CORE_TEST_DB_PASSWORD", "cadence");

    @SuppressWarnings("resource")
    private static final PostgreSQLContainer<?> POSTGRES =
        new PostgreSQLContainer<>(DockerImageName.parse("postgres:16-alpine"))
            .withDatabaseName("cadence_boundary_test")
            .withUsername("cadence")
            .withPassword("cadence")
            .withInitScript(null);

    static {
        if (EXTERNAL_URL == null || EXTERNAL_URL.isBlank()) {
            POSTGRES.start();
        }
    }

    @DynamicPropertySource
    static void datasource(DynamicPropertyRegistry registry) {
        if (EXTERNAL_URL != null && !EXTERNAL_URL.isBlank()) {
            registry.add("spring.datasource.url", () -> EXTERNAL_URL);
            registry.add("spring.datasource.username", () -> EXTERNAL_USER);
            registry.add("spring.datasource.password", () -> EXTERNAL_PASSWORD);
        } else {
            registry.add("spring.datasource.url", POSTGRES::getJdbcUrl);
            registry.add("spring.datasource.username", POSTGRES::getUsername);
            registry.add("spring.datasource.password", POSTGRES::getPassword);
        }
    }
}
