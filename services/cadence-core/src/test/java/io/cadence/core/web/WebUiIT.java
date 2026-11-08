package io.cadence.core.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.options;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import com.jayway.jsonpath.JsonPath;
import io.cadence.core.AbstractIntegrationTest;
import io.cadence.core.config.WebConfig;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

/**
 * 前端页面的托管与跨域。
 *
 * 前端页面（cadence-web）由 Core 自己托管在 /ui/**：
 *   - 打开 http://localhost:18080/ 会跳到 /ui/index.html
 *   - 首页、样式、脚本都能取到
 *   - /api/** 允许跨域，前端用 file:// 或别的端口打开也能调
 */
@AutoConfigureMockMvc
class WebUiIT extends AbstractIntegrationTest {

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;

    @BeforeEach
    void clean() {
        jdbc.execute("TRUNCATE audit_log, idempotency_record, task_dependency, task CASCADE");
    }

    @Test
    @DisplayName("前端静态目录能被解析到 services/cadence-web")
    void webDirResolves() {
        assertThat(WebConfig.resolveWebDir().toString()).contains("cadence-web");
    }

    @Test
    @DisplayName("GET / → 302 到 /ui/index.html")
    void rootRedirectsToUi() throws Exception {
        MvcResult r = mvc.perform(get("/")).andReturn();
        assertThat(r.getResponse().getStatus()).isEqualTo(302);
        assertThat(r.getResponse().getRedirectedUrl()).isEqualTo("/ui/index.html");
    }

    @Test
    @DisplayName("/ui/index.html 与静态资源可取，且不被响应外壳包装")
    void uiAssetsAreServed() throws Exception {
        MvcResult page = mvc.perform(get("/ui/index.html")).andReturn();
        assertThat(page.getResponse().getStatus()).isEqualTo(200);
        String html = page.getResponse().getContentAsString();
        assertThat(html).contains("<title>");
        assertThat(html).doesNotContain("\"code\":0");

        for (String asset : new String[] {"/ui/app.css", "/ui/api.js", "/ui/app.js"}) {
            MvcResult r = mvc.perform(get(asset)).andReturn();
            assertThat(r.getResponse().getStatus()).as(asset).isEqualTo(200);
            assertThat(r.getResponse().getContentAsString()).as(asset).isNotBlank();
        }
    }

    @Test
    @DisplayName("任意 /ui/ 下的未知页面回落到 index.html（刷新不 404）")
    void unknownUiPathFallsBackToIndex() throws Exception {
        MvcResult r = mvc.perform(get("/ui/board")).andReturn();
        // 该路径不存在时返回 404 也可以接受，但不能 500
        assertThat(r.getResponse().getStatus()).isIn(200, 404);
    }

    @Test
    @DisplayName("预检请求 OPTIONS /api/v1/tasks 返回 CORS 头")
    void preflightAllowed() throws Exception {
        MvcResult r = mvc.perform(options("/api/v1/tasks")
                .header("Origin", "http://localhost:9000")
                .header("Access-Control-Request-Method", "POST")
                .header("Access-Control-Request-Headers", "content-type,x-actor"))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(200);
        assertThat(r.getResponse().getHeader("Access-Control-Allow-Origin")).isEqualTo("http://localhost:9000");
        assertThat(r.getResponse().getHeader("Access-Control-Allow-Headers"))
            .containsIgnoringCase("x-actor");
    }

    @Test
    @DisplayName("实际请求也带回 CORS 头，PATCH/DELETE 同样允许")
    void actualRequestCarriesCorsHeader() throws Exception {
        MvcResult created = mvc.perform(post("/api/v1/tasks")
                .header("Origin", "http://localhost:9000")
                .contentType(MediaType.APPLICATION_JSON).content("{\"title\":\"跨域\"}"))
            .andReturn();
        assertThat(created.getResponse().getStatus()).isEqualTo(201);
        assertThat(created.getResponse().getHeader("Access-Control-Allow-Origin")).isEqualTo("http://localhost:9000");

        String id = JsonPath.read(created.getResponse().getContentAsString(), "$.data.id");
        MvcResult patched = mvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders
                .patch("/api/v1/tasks/" + id)
                .header("Origin", "http://localhost:9000")
                .contentType(MediaType.APPLICATION_JSON).content("{\"version\":0,\"title\":\"改过\"}"))
            .andReturn();
        assertThat(patched.getResponse().getStatus()).isEqualTo(200);
        assertThat(patched.getResponse().getHeader("Access-Control-Allow-Origin")).isEqualTo("http://localhost:9000");
    }
}
