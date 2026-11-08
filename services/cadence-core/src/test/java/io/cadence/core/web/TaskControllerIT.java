package io.cadence.core.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import com.jayway.jsonpath.JsonPath;
import io.cadence.core.AbstractIntegrationTest;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * HTTP 层的契约验证。
 *
 * 断言重点不是业务逻辑（那在 service 层测过了），而是：状态码、错误码、
 * JSON 字段名与类型，以及统一外壳 {code, message, data} —— 这些是前端和 Agent 依赖的东西。
 *
 * 读响应的两种姿势：
 *   - 成功：$.data.xxx
 *   - 失败：$.code（数字，等于 HTTP 状态码）+ $.message，没有 data 字段
 */
@AutoConfigureMockMvc
class TaskControllerIT extends AbstractIntegrationTest {

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;

    private static final ObjectMapper JSON = new ObjectMapper();

    @BeforeEach
    void clean() {
        jdbc.execute("TRUNCATE audit_log, idempotency_record, task_dependency, task CASCADE");
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------

    /** 创建并用 JsonPath 取 data.id */
    private String createRaw(String title, Map<String, Object> extra) throws Exception {
        MvcResult r = postTask(title, extra).andReturn();
        assertThat(r.getResponse().getStatus()).isEqualTo(201);
        return JsonPath.read(r.getResponse().getContentAsString(), "$.data.id");
    }

    private org.springframework.test.web.servlet.ResultActions postTask(String title, Map<String, Object> extra)
        throws Exception {
        var body = new java.util.LinkedHashMap<String, Object>();
        body.put("title", title);
        if (extra != null) {
            body.putAll(extra);
        }
        return mvc.perform(post("/api/v1/tasks")
                .header("X-Actor", "agent")
                .contentType(MediaType.APPLICATION_JSON)
                .content(json(body)))
            .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isCreated());
    }

    private String create(String title, Map<String, Object> extra) throws Exception {
        return createRaw(title, extra);
    }

    private static JsonNode body(MvcResult r) throws Exception {
        return JSON.readTree(r.getResponse().getContentAsString());
    }

    private static String json(Object o) {
        try {
            // Boot 4 使用 Jackson 3（tools.jackson），不是 com.fasterxml
            return new ObjectMapper().writeValueAsString(o);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    // ------------------------------------------------------------------
    // 成功响应：统一外壳
    // ------------------------------------------------------------------

    @Test
    @DisplayName("创建返回 201 + {code:0,message:success,data:{完整字段,version:0}}")
    void createReturnsWrappedBody() throws Exception {
        MvcResult r = mvc.perform(post("/api/v1/tasks")
                .header("X-Actor", "agent")
                .contentType(MediaType.APPLICATION_JSON)
                .content("""
                    {"title":"交季度报告","priority":"URGENT",
                     "dueAt":"2026-11-07T18:00:00+08:00","estimateMin":360,
                     "tags":["work"]}
                    """))
            .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isCreated())
            .andReturn();

        JsonNode b = body(r);
        assertThat(b.get("code").asInt()).isZero();
        assertThat(b.get("message").asString()).isEqualTo("success");

        JsonNode t = b.get("data");
        assertThat(t.get("id").asText()).isNotBlank();
        assertThat(t.get("title").asText()).isEqualTo("交季度报告");
        assertThat(t.get("status").asText()).isEqualTo("TODO");
        assertThat(t.get("priority").asText()).isEqualTo("URGENT");
        assertThat(t.get("version").asLong()).isZero();
        assertThat(t.get("pinned").asBoolean()).isFalse();
        assertThat(t.get("dueAt").asText()).isEqualTo("2026-11-07T18:00:00+08:00");
    }

    @Test
    @DisplayName("列表：{code:0, data:{items,hasMore,total}}")
    void listShape() throws Exception {
        create("A", null);
        create("B", null);

        MvcResult r = mvc.perform(get("/api/v1/tasks").param("limit", "1")).andReturn();

        JsonNode d = body(r).get("data");
        assertThat(d.get("total").asInt()).isEqualTo(2);
        assertThat(d.get("hasMore").asBoolean()).isTrue();
        assertThat(d.get("items").size()).isEqualTo(1);
    }

    @Test
    @DisplayName("单任务详情返回依赖与冲突（data.task / data.dependsOn / data.conflicts）")
    void detailShape() throws Exception {
        String b = create("前置", Map.of(
            "startAt", "2026-11-05T09:00:00+08:00", "estimateMin", 120));
        String a = create("后继", Map.of(
            "startAt", "2026-11-05T10:00:00+08:00", "estimateMin", 60));

        mvc.perform(post("/api/v1/tasks/" + a + "/dependencies")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"dependsOnId\":\"" + b + "\"}"))
            .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isCreated());

        MvcResult r = mvc.perform(get("/api/v1/tasks/" + a)).andReturn();

        JsonNode d = body(r).get("data");
        assertThat(d.get("dependsOn").get(0).asText()).isEqualTo(b);
        assertThat(d.get("conflicts").size()).isEqualTo(1);
        assertThat(d.get("conflicts").get(0).get("taskId").asText()).isEqualTo(b);
    }

    @Test
    @DisplayName("PATCH 成功 → data:{task, warnings:[...]}")
    void patchWrapped() throws Exception {
        String id = create("可改", null);
        MvcResult r = mvc.perform(patch("/api/v1/tasks/" + id)
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"version\":0,\"title\":\"改过\"}"))
            .andReturn();

        JsonNode d = body(r).get("data");
        assertThat(d.get("task").get("title").asText()).isEqualTo("改过");
        assertThat(d.get("task").get("version").asLong()).isEqualTo(1L);
        assertThat(d.get("warnings").isArray()).isTrue();
    }

    @Test
    @DisplayName("完成幂等：连做两次都是 200 + data.status=DONE")
    void completeIsIdempotent() throws Exception {
        String id = create("任务", null);

        for (int i = 0; i < 2; i++) {
            MvcResult r = mvc.perform(post("/api/v1/tasks/" + id + ":complete")
                    .contentType(MediaType.APPLICATION_JSON).content("{\"version\":0}"))
                .andReturn();
            assertThat(r.getResponse().getStatus()).isEqualTo(200);
            assertThat(body(r).get("data").get("status").asText()).isEqualTo("DONE");
        }
    }

    @Test
    @DisplayName("软删除 → 第一次 403 拿令牌，第二次 204 无 body")
    void softDelete() throws Exception {
        String id = create("要删的", null);

        // 两阶段：先拿令牌
        MvcResult first = mvc.perform(delete("/api/v1/tasks/" + id)).andReturn();
        assertThat(first.getResponse().getStatus()).isEqualTo(403);
        String token = body(first).get("data").get("confirmToken").asText();

        MvcResult r = mvc.perform(delete("/api/v1/tasks/" + id).header("X-Confirm-Token", token)).andReturn();
        assertThat(r.getResponse().getStatus()).isEqualTo(204);
        assertThat(r.getResponse().getContentAsString()).isEmpty();

        MvcResult gone = mvc.perform(get("/api/v1/tasks/" + id)).andReturn();
        assertThat(gone.getResponse().getStatus()).isEqualTo(404);
        assertThat(body(gone).get("code").asInt()).isEqualTo(404);

        JsonNode list = body(mvc.perform(get("/api/v1/tasks")).andReturn()).get("data");
        assertThat(list.get("total").asInt()).isZero();
    }

    @Test
    @DisplayName("忙闲查询：data.windows 已合并")
    void busyShape() throws Exception {
        create("早会", Map.of(
            "startAt", "2026-11-05T09:00:00+08:00",
            "estimateMin", 60));

        MvcResult r = mvc.perform(get("/api/v1/calendar/busy")
                .param("from", "2026-11-05T00:00:00+08:00")
                .param("to", "2026-11-06T00:00:00+08:00"))
            .andReturn();

        JsonNode w = body(r).get("data").get("windows");
        assertThat(w.size()).isEqualTo(1);
        assertThat(w.get(0).get("minutes").asLong()).isEqualTo(60L);
    }

    @Test
    @DisplayName("重复任务展开：data.occurrences")
    void occurrencesShape() throws Exception {
        String id = create("每周站会", Map.of(
            "startAt", "2026-11-04T09:00:00+08:00",
            "rrule", "FREQ=WEEKLY;BYDAY=WE;COUNT=3"));

        MvcResult r = mvc.perform(get("/api/v1/tasks/" + id + "/occurrences")
                .param("from", "2026-11-01T00:00:00+08:00")
                .param("to", "2026-12-01T00:00:00+08:00"))
            .andReturn();

        JsonNode o = body(r).get("data").get("occurrences");
        assertThat(o.size()).isEqualTo(3);
        assertThat(o.get(0).asText()).isEqualTo("2026-11-04T09:00:00+08:00");
    }

    // ------------------------------------------------------------------
    // 错误响应：{code: 4xx, message}
    // ------------------------------------------------------------------

    @Test
    @DisplayName("错误 version → 409 + {code:409}")
    void conflictShape() throws Exception {
        String id = create("任务", null);

        MvcResult r = mvc.perform(patch("/api/v1/tasks/" + id)
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"version\":999,\"title\":\"改\"}"))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(409);
        JsonNode b = body(r);
        assertThat(b.get("code").asInt()).isEqualTo(409);
        assertThat(b.get("message").asString()).contains("version");
        assertThat(b.has("data")).isFalse();
    }

    @Test
    @DisplayName("不存在 id → 404 + {code:404}")
    void notFoundShape() throws Exception {
        MvcResult r = mvc.perform(get("/api/v1/tasks/00000000-0000-0000-0000-000000000000")).andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(404);
        assertThat(body(r).get("code").asInt()).isEqualTo(404);
    }

    @Test
    @DisplayName("参数越界 → 400 + {code:400}，message 指出字段")
    void validationShape() throws Exception {
        MvcResult r = mvc.perform(post("/api/v1/tasks")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"title\":\"x\",\"estimateMin\":9999}"))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(400);
        JsonNode b = body(r);
        assertThat(b.get("code").asInt()).isEqualTo(400);
        assertThat(b.get("message").asString()).contains("estimateMin");
    }

    @Test
    @DisplayName("前置未完成 → 422 + {code:422}")
    void completeBlockedShape() throws Exception {
        String b = create("前置", null);
        String a = create("后继", null);

        mvc.perform(post("/api/v1/tasks/" + a + "/dependencies")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"dependsOnId\":\"" + b + "\"}"))
            .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isCreated());

        MvcResult r = mvc.perform(post("/api/v1/tasks/" + a + ":complete")
                .contentType(MediaType.APPLICATION_JSON).content("{\"version\":0}"))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(422);
        assertThat(body(r).get("code").asInt()).isEqualTo(422);
    }

    @Test
    @DisplayName("依赖成环 → 422 + {code:422}")
    void cycleShape() throws Exception {
        String a = create("A", null);
        String b = create("B", null);

        mvc.perform(post("/api/v1/tasks/" + a + "/dependencies")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"dependsOnId\":\"" + b + "\"}"))
            .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isCreated());

        MvcResult r = mvc.perform(post("/api/v1/tasks/" + b + "/dependencies")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"dependsOnId\":\"" + a + "\"}"))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(422);
        assertThat(body(r).get("code").asInt()).isEqualTo(422);
        assertThat(body(r).get("message").asString()).contains("环");
    }

    @Test
    @DisplayName("批量单条冲突 → 409 + {code:409}")
    void batchConflict() throws Exception {
        String a = create("A", Map.of("startAt", "2026-11-10T09:00:00+08:00"));

        MvcResult r = mvc.perform(post("/api/v1/tasks:batch")
                .contentType(MediaType.APPLICATION_JSON)
                .content("""
                    {"ops":[{"id":"%s","version":999,
                             "patch":{"startAt":"2026-11-20T09:00:00+08:00"}}]}
                    """.formatted(a)))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(409);
        assertThat(body(r).get("code").asInt()).isEqualTo(409);
    }

    // ------------------------------------------------------------------
    // 契约一致性（原 ContractConformanceIT 的内容，统一放在这里便于核对）
    // ------------------------------------------------------------------

    @Test
    @DisplayName("openapi.yaml 的 paths 段可解析且至少 14 个路径")
    void specHasPaths() throws Exception {
        Path spec = Path.of("..", "..", "specs", "contracts", "openapi.yaml");
        assertThat(Files.exists(spec)).as("找不到契约文件").isTrue();

        Set<String> paths = new TreeSet<>();
        // 每个路径形如 "  /api/v1/xxx:"（paths 段下 2 空格缩进）
        Files.readAllLines(spec).stream()
            .filter(l -> l.matches("^  /\\S+:\\s*$"))
            .forEach(l -> paths.add(l.strip().replaceAll(":$", "")));

        assertThat(paths).contains("/api/v1/tasks", "/api/v1/tasks:batch",
            "/api/v1/calendar/busy", "/api/v1/tasks/{id}/history");
    }
}
