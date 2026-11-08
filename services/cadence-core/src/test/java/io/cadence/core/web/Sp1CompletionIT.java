package io.cadence.core.web;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import io.cadence.core.AbstractIntegrationTest;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
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

import static org.assertj.core.api.Assertions.assertThat;

/**
 * FR-2/4/8/10/11 在 HTTP 层的契约验证。
 *
 * 所有响应都走统一外壳：成功读 $.data.*，失败读 $.code / $.message。
 */
@AutoConfigureMockMvc
class Sp1CompletionIT extends AbstractIntegrationTest {

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

    private static JsonNode body(MvcResult r) throws Exception {
        return JSON.readTree(r.getResponse().getContentAsString());
    }

    private JsonNode data(MvcResult r) throws Exception {
        return body(r).get("data");
    }

    private String json(Object o) {
        try {
            return JSON.writeValueAsString(o);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private String create(String title, Map<String, Object> extra) throws Exception {
        var b = new LinkedHashMap<String, Object>();
        b.put("title", title);
        if (extra != null) b.putAll(extra);

        MvcResult r = mvc.perform(post("/api/v1/tasks")
                .header("X-Actor", "agent")
                .contentType(MediaType.APPLICATION_JSON).content(json(b)))
            .andReturn();
        assertThat(r.getResponse().getStatus()).as("创建失败: " + r.getResponse().getContentAsString()).isEqualTo(201);
        return data(r).get("id").asText();
    }

    private String create(String title) throws Exception {
        return create(title, null);
    }

    private MvcResult stateCall(String id, String action, long version) throws Exception {
        return mvc.perform(post("/api/v1/tasks/" + id + ":" + action)
                .contentType(MediaType.APPLICATION_JSON).content("{\"version\":" + version + "}"))
            .andReturn();
    }

    // ------------------------------------------------------------------
    // 状态机
    // ------------------------------------------------------------------

    @Test
    @DisplayName("FR-2：start / cancel / reopen 三个状态端点")
    void stateMachineEndpoints() throws Exception {
        String id = create("生命周期任务");

        MvcResult start = stateCall(id, "start", 0);
        assertThat(start.getResponse().getStatus()).isEqualTo(200);
        assertThat(data(start).get("status").asText()).isEqualTo("DOING");

        MvcResult cancel = stateCall(id, "cancel", 1);
        assertThat(cancel.getResponse().getStatus()).isEqualTo(200);
        assertThat(data(cancel).get("status").asText()).isEqualTo("CANCELLED");

        MvcResult reopen = stateCall(id, "reopen", 2);
        assertThat(reopen.getResponse().getStatus()).isEqualTo(200);
        assertThat(data(reopen).get("status").asText()).isEqualTo("TODO");
    }

    @Test
    @DisplayName("FR-2：非法流转 → 422 + {code:422}，message 说明原因")
    void illegalTransitionShape() throws Exception {
        String id = create("已完成任务");
        assertThat(stateCall(id, "complete", 0).getResponse().getStatus()).isEqualTo(200);

        MvcResult r = stateCall(id, "start", 1);
        assertThat(r.getResponse().getStatus()).isEqualTo(422);
        JsonNode b = body(r);
        assertThat(b.get("code").asInt()).isEqualTo(422);
        assertThat(b.get("message").asString()).contains("TODO");
    }

    // ------------------------------------------------------------------
    // 删除两阶段
    // ------------------------------------------------------------------

    @Test
    @DisplayName("FR-4：删除需要确认令牌（两阶段），第一次 403 + 令牌")
    void deleteRequiresConfirm() throws Exception {
        String id = create("要删的任务");

        // 第一阶段：不带令牌 → 403，响应体里带令牌
        MvcResult r = mvc.perform(delete("/api/v1/tasks/" + id)).andReturn();
        assertThat(r.getResponse().getStatus()).isEqualTo(403);
        JsonNode b = body(r);
        assertThat(b.get("code").asInt()).isEqualTo(403);
        String token = b.get("data").get("confirmToken").asText();
        assertThat(token).startsWith("ct_");

        // 任务还在
        assertThat(mvc.perform(get("/api/v1/tasks/" + id)).andReturn().getResponse().getStatus()).isEqualTo(200);

        // 第二阶段：带令牌 → 204
        MvcResult done = mvc.perform(delete("/api/v1/tasks/" + id).header("X-Confirm-Token", token)).andReturn();
        assertThat(done.getResponse().getStatus()).isEqualTo(204);

        assertThat(mvc.perform(get("/api/v1/tasks/" + id)).andReturn().getResponse().getStatus()).isEqualTo(404);
    }

    @Test
    @DisplayName("FR-4：令牌只能用一次")
    void confirmTokenSingleUse() throws Exception {
        String id = create("任务");

        MvcResult r = mvc.perform(delete("/api/v1/tasks/" + id)).andReturn();
        String token = body(r).get("data").get("confirmToken").asText();

        assertThat(mvc.perform(delete("/api/v1/tasks/" + id).header("X-Confirm-Token", token))
            .andReturn().getResponse().getStatus()).isEqualTo(204);

        // 第二次用同一令牌 → 任务已删除，404
        assertThat(mvc.perform(delete("/api/v1/tasks/" + id).header("X-Confirm-Token", token))
            .andReturn().getResponse().getStatus()).isEqualTo(404);
    }

    // ------------------------------------------------------------------
    // 幂等
    // ------------------------------------------------------------------

    @Test
    @DisplayName("FR-10：同一幂等键只创建一个任务，重放带 Idempotent-Replayed 标记")
    void idempotentCreate() throws Exception {
        String key = "idem-" + UUID.randomUUID();
        String payload = json(Map.of("title", "幂等任务"));

        MvcResult r1 = mvc.perform(post("/api/v1/tasks")
                .header("X-Idempotency-Key", key)
                .contentType(MediaType.APPLICATION_JSON).content(payload))
            .andReturn();
        String id1 = data(r1).get("id").asText();

        MvcResult r2 = mvc.perform(post("/api/v1/tasks")
                .header("X-Idempotency-Key", key)
                .contentType(MediaType.APPLICATION_JSON).content(payload))
            .andReturn();

        assertThat(r2.getResponse().getHeader("Idempotent-Replayed")).isEqualTo("true");
        assertThat(body(r2).get("code").asInt()).isZero();
        assertThat(data(r2).get("id").asText()).isEqualTo(id1);

        JsonNode list = data(mvc.perform(get("/api/v1/tasks")).andReturn());
        assertThat(list.get("total").asInt()).isEqualTo(1);
    }

    @Test
    @DisplayName("FR-10：不同幂等键创建两个任务")
    void differentKeysCreateDifferentTasks() throws Exception {
        String payload = json(Map.of("title", "同名任务"));

        mvc.perform(post("/api/v1/tasks")
            .header("X-Idempotency-Key", "k1").contentType(MediaType.APPLICATION_JSON).content(payload));
        mvc.perform(post("/api/v1/tasks")
            .header("X-Idempotency-Key", "k2").contentType(MediaType.APPLICATION_JSON).content(payload));

        assertThat(data(mvc.perform(get("/api/v1/tasks")).andReturn()).get("total").asInt()).isEqualTo(2);
    }

    // ------------------------------------------------------------------
    // 冲突警告
    // ------------------------------------------------------------------

    @Test
    @DisplayName("FR-8：更新返回 data.warnings[] 而不是阻止写入")
    void patchReturnsWarnings() throws Exception {
        create("长任务", Map.of("startAt", "2026-11-20T09:00:00+08:00", "estimateMin", 480));
        String b = create("冲突任务", Map.of("startAt", "2026-11-20T15:00:00+08:00", "estimateMin", 30));

        MvcResult r = mvc.perform(patch("/api/v1/tasks/" + b)
                .contentType(MediaType.APPLICATION_JSON)
                .content(json(Map.of("version", 0, "startAt", "2026-11-20T12:00:00+08:00"))))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(200);
        JsonNode d = data(r);
        assertThat(d.get("task").get("startAt").asText()).isEqualTo("2026-11-20T12:00:00+08:00");
        assertThat(d.get("warnings").size()).isEqualTo(1);
        assertThat(d.get("warnings").get(0).get("code").asText()).isEqualTo("TIME_CONFLICT");
    }

    @Test
    @DisplayName("FR-8：无冲突时 warnings 为空数组")
    void patchNoWarnings() throws Exception {
        String id = create("独立任务");
        MvcResult r = mvc.perform(patch("/api/v1/tasks/" + id)
                .contentType(MediaType.APPLICATION_JSON)
                .content(json(Map.of("version", 0, "title", "改名"))))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(200);
        assertThat(data(r).get("warnings").isArray()).isTrue();
        assertThat(data(r).get("warnings").size()).isZero();
    }

    // ------------------------------------------------------------------
    // 审计
    // ------------------------------------------------------------------

    @Test
    @DisplayName("FR-11：审计可通过 API 查询，按时间倒序，data 是数组")
    void auditQueryable() throws Exception {
        String id = create("审计任务");
        mvc.perform(patch("/api/v1/tasks/" + id)
                .header("X-Trace-Id", "tr_777")
                .contentType(MediaType.APPLICATION_JSON)
                .content(json(Map.of("version", 0, "title", "改过"))))
            .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isOk());

        MvcResult r = mvc.perform(get("/api/v1/tasks/" + id + "/history")).andReturn();
        JsonNode h = data(r);

        assertThat(h.isArray()).isTrue();
        assertThat(h.size()).isEqualTo(2);
        assertThat(h.get(0).get("action").asText()).isEqualTo("UPDATE");
        assertThat(h.get(0).get("traceId").asText()).isEqualTo("tr_777");
        assertThat(h.get(1).get("action").asText()).isEqualTo("CREATE");
    }
}
