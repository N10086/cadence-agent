package io.cadence.core.web;

import static org.assertj.core.api.Assertions.assertThat;

import io.cadence.core.AbstractIntegrationTest;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;

/**
 * 路由自检：把所有已注册的端点打印出来并被断言。
 *
 * 为什么值得单独写一个测试：
 * Spring MVC 的路径匹配对 ":" 的处理不直观，写错时表现为 404（NoResourceFoundException），
 * 而错误信息完全指不到 Controller —— 排查成本极高。这个测试把路由表变成可断言的。
 */
class RouteMappingIT extends AbstractIntegrationTest {

    @Autowired
    @org.springframework.beans.factory.annotation.Qualifier("requestMappingHandlerMapping")
    RequestMappingHandlerMapping mapping;

    @Test
    @DisplayName("打印全部已注册路由")
    void dumpRoutes() {
        mapping.getHandlerMethods().forEach((info, method) -> {
            System.out.println("ROUTE  " + info);
        });
    }

    @Test
    @DisplayName("关键端点都已注册")
    void requiredEndpointsRegistered() {
        var patterns = mapping.getHandlerMethods().keySet().stream()
            .map(Object::toString)
            .toList();

        assertThat(patterns).anyMatch(p -> p.contains("/api/v1/tasks"));
    }
}
