package io.cadence.core.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import io.cadence.core.AbstractIntegrationTest;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;
import java.util.TreeSet;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.test.web.servlet.MockMvc;

/**
 * FR-12：springdoc 导出的 OpenAPI 与实际运行时一致。
 *
 * 与 ContractConformanceIT 的分工：
 *   ContractConformanceIT → 手写契约 yaml  vs  运行时路由
 *   本测试               → springdoc 导出    vs  运行时路由
 *
 * 两者结合，保证"文档 - 生成物 - 实现"三者不漂移。
 */
@AutoConfigureMockMvc
class OpenApiExportIT extends AbstractIntegrationTest {

    @Autowired MockMvc mvc;

    @Test
    @DisplayName("/v3/api-docs 可访问且包含全部业务路径")
    void apiDocsAvailable() throws Exception {
        var r = mvc.perform(get("/v3/api-docs"))
            .andExpect(status().isOk())
            .andReturn();

        String body = r.getResponse().getContentAsString();
        assertThat(body).contains("\"openapi\"");
        assertThat(body).contains("/api/v1/tasks");
        assertThat(body).contains("/api/v1/tasks:batch");
        assertThat(body).contains("/api/v1/calendar/busy");
        assertThat(body).contains("operationId");
    }

    @Test
    @DisplayName("导出的路径集合与运行时注册的路径集合一致")
    void exportedPathsMatchRuntime() throws Exception {
        String body = mvc.perform(get("/v3/api-docs"))
            .andExpect(status().isOk())
            .andReturn().getResponse().getContentAsString();

        var mapper = new tools.jackson.databind.ObjectMapper();
        var root = mapper.readTree(body);
        Set<String> exported = new TreeSet<>();
        root.get("paths").propertyNames().forEach(exported::add);

        Set<String> expected = new TreeSet<>(Set.of(
            "/api/v1/tasks",
            "/api/v1/tasks/{id}",
            "/api/v1/tasks/{id}:complete",
            "/api/v1/tasks/{id}:start",
            "/api/v1/tasks/{id}:reopen",
            "/api/v1/tasks/{id}:cancel",
            "/api/v1/tasks:batch",
            "/api/v1/tasks/{id}/dependencies",
            "/api/v1/tasks/{id}/dependencies/{dependsOnId}",
            "/api/v1/tasks/{id}/occurrences",
            "/api/v1/tasks/{id}/history",
            "/api/v1/calendar/busy"
        ));

        assertThat(exported).containsAll(expected);
    }

    @Test
    @DisplayName("把导出的 OpenAPI 落盘到 docs/openapi.exported.json，便于人工比对")
    void dumpExportedSpec() throws Exception {
        String body = mvc.perform(get("/v3/api-docs"))
            .andExpect(status().isOk())
            .andReturn().getResponse().getContentAsString();

        Path out = Path.of("..", "..", "docs", "openapi.exported.json");
        Files.createDirectories(out.getParent());
        Files.writeString(out, body);

        assertThat(Files.exists(out)).isTrue();
        assertThat(Files.size(out)).isGreaterThan(1000);
    }
}
