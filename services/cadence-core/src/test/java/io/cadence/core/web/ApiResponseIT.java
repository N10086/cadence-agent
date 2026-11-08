package io.cadence.core.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import io.cadence.core.AbstractIntegrationTest;
import io.cadence.core.web.error.ApiError;
import io.cadence.core.web.error.ErrorCode;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.Map;
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
 * 统一响应外壳 {code, message, data} 的验收。
 *
 * 覆盖三条与包装相关的通道，任何一条漏包，前端就得写两套解析：
 *   1. 正常返回（TaskResponse / TaskListResponse / Map / List）
 *   2. 业务异常（GlobalExceptionHandler → ApiError）
 *   3. Spring 内置异常（404 无路由 / 405 方法不允许 / 参数类型不匹配 → ErrorResponse）
 * 另外还验证 /v3/api-docs 与 /actuator 不被包装（它们有自己的格式契约）。
 */
@AutoConfigureMockMvc
class ApiResponseIT extends AbstractIntegrationTest {

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

    private MvcResult get(String url, String... params) throws Exception {
        var req = org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get(url);
        if (params.length >= 2) {
            req = req.queryParam(params[0], params[1]);
        }
        return mvc.perform(req).andReturn();
    }

    private String createTask(String title) throws Exception {
        MvcResult r = mvc.perform(post("/api/v1/tasks")
                .contentType(MediaType.APPLICATION_JSON)
                .content(JSON.writeValueAsString(Map.of("title", title))))
            .andReturn();
        assertThat(r.getResponse().getStatus()).isEqualTo(201);
        return body(r).get("data").get("id").asText();
    }

    // ------------------------------------------------------------------
    // 1. 正常返回
    // ------------------------------------------------------------------

    @Test
    @DisplayName("列表 → {code:0, message:success, data:{items,hasMore,total}}")
    void successIsWrapped() throws Exception {
        MvcResult r = get("/api/v1/tasks");

        assertThat(r.getResponse().getStatus()).isEqualTo(200);
        JsonNode b = body(r);
        assertThat(b.get("code").asInt()).isZero();
        assertThat(b.get("message").asString()).isEqualTo("success");
        assertThat(b.get("data").get("items").isArray()).isTrue();
        assertThat(b.get("data").get("total").asInt()).isZero();
    }

    @Test
    @DisplayName("创建 → 201 + data 里是任务本体，version=0")
    void createdIsWrapped() throws Exception {
        MvcResult r = mvc.perform(post("/api/v1/tasks")
                .contentType(MediaType.APPLICATION_JSON).content("{\"title\":\"包装测试\"}"))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(201);
        JsonNode d = body(r).get("data");
        assertThat(d.get("title").asText()).isEqualTo("包装测试");
        assertThat(d.get("version").asLong()).isZero();
        assertThat(d.get("status").asText()).isEqualTo("TODO");
    }

    @Test
    @DisplayName("直接返回 List 的接口（审计历史）也包在 data 下")
    void listBodyIsWrapped() throws Exception {
        String id = createTask("有历史的任务");
        mvc.perform(patch("/api/v1/tasks/" + id)
                .contentType(MediaType.APPLICATION_JSON).content("{\"version\":0,\"title\":\"改过\"}"))
            .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isOk());

        MvcResult r = get("/api/v1/tasks/" + id + "/history");
        JsonNode b = body(r);
        assertThat(b.get("code").asInt()).isZero();
        assertThat(b.get("data").isArray()).isTrue();
        assertThat(b.get("data").size()).isEqualTo(2);
        assertThat(b.get("data").get(0).get("action").asText()).isEqualTo("UPDATE");
    }

    // ------------------------------------------------------------------
    // 2. 业务异常
    // ------------------------------------------------------------------

    @Test
    @DisplayName("400 参数校验失败 → {code:400, message 指出字段, 无 data}")
    void validationErrorIsWrapped() throws Exception {
        MvcResult r = mvc.perform(post("/api/v1/tasks")
                .contentType(MediaType.APPLICATION_JSON)
                .content("{\"title\":\"x\",\"estimateMin\":9999}"))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(400);
        JsonNode b = body(r);
        assertThat(b.get("code").asInt()).isEqualTo(400);
        assertThat(b.get("message").asString()).contains("estimateMin");
        assertThat(b.has("data")).isFalse();
    }

    @Test
    @DisplayName("409 版本冲突 → {code:409}")
    void conflictIsWrapped() throws Exception {
        String id = createTask("冲突任务");
        MvcResult r = mvc.perform(patch("/api/v1/tasks/" + id)
                .contentType(MediaType.APPLICATION_JSON).content("{\"version\":999,\"title\":\"x\"}"))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(409);
        assertThat(body(r).get("code").asInt()).isEqualTo(409);
        assertThat(body(r).get("message").asString()).contains("version");
    }

    @Test
    @DisplayName("422 非法流转 → {code:422}")
    void unprocessableIsWrapped() throws Exception {
        String id = createTask("非法流转");
        mvc.perform(post("/api/v1/tasks/" + id + ":complete")
                .contentType(MediaType.APPLICATION_JSON).content("{\"version\":0}"))
            .andExpect(org.springframework.test.web.servlet.result.MockMvcResultMatchers.status().isOk());

        MvcResult r = mvc.perform(post("/api/v1/tasks/" + id + ":start")
                .contentType(MediaType.APPLICATION_JSON).content("{\"version\":1}"))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(422);
        assertThat(body(r).get("code").asInt()).isEqualTo(422);
    }

    @Test
    @DisplayName("404 任务不存在 → {code:404}")
    void notFoundIsWrapped() throws Exception {
        MvcResult r = get("/api/v1/tasks/00000000-0000-0000-0000-000000000000");

        assertThat(r.getResponse().getStatus()).isEqualTo(404);
        assertThat(body(r).get("code").asInt()).isEqualTo(404);
    }

    @Test
    @DisplayName("DELETE 两阶段：第一次 403，data 里带 confirmToken")
    void confirmRequiredCarriesToken() throws Exception {
        String id = createTask("待删除");
        MvcResult r = mvc.perform(delete("/api/v1/tasks/" + id)).andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(403);
        JsonNode b = body(r);
        assertThat(b.get("code").asInt()).isEqualTo(403);
        assertThat(b.get("data").get("confirmToken").asText()).startsWith("ct_");
        assertThat(b.get("data").get("snapshot").get("title").asText()).isEqualTo("待删除");
    }

    // ------------------------------------------------------------------
    // 3. Spring 内置异常
    // ------------------------------------------------------------------

    @Test
    @DisplayName("405 方法不允许 → {code:405}，且带 Allow 头")
    void methodNotAllowedIsWrapped() throws Exception {
        MvcResult r = mvc.perform(post("/api/v1/calendar/busy")
                .contentType(MediaType.APPLICATION_JSON).content("{}"))
            .andReturn();

        assertThat(r.getResponse().getStatus()).isEqualTo(405);
        assertThat(body(r).get("code").asInt()).isEqualTo(405);
        assertThat(r.getResponse().getHeader("Allow")).isNotNull();
    }

    @Test
    @DisplayName("400 查询参数类型不对 → {code:400}")
    void badQueryParamIsWrapped() throws Exception {
        MvcResult r = get("/api/v1/tasks", "limit", "abc");

        assertThat(r.getResponse().getStatus()).isEqualTo(400);
        JsonNode b = body(r);
        assertThat(b.get("code").asInt()).isEqualTo(400);
        assertThat(b.get("message").asString()).contains("limit");
    }

    // ------------------------------------------------------------------
    // 范围：非业务接口保持原格式
    // ------------------------------------------------------------------

    @Test
    @DisplayName("/v3/api-docs 不被包装，仍是 OpenAPI 文档")
    void apiDocsNotWrapped() throws Exception {
        MvcResult r = get("/v3/api-docs");

        assertThat(r.getResponse().getStatus()).isEqualTo(200);
        JsonNode b = body(r);
        assertThat(b.has("openapi")).isTrue();
        assertThat(b.has("code")).isFalse();
    }

    @Test
    @DisplayName("/actuator/health 不被包装")
    void actuatorNotWrapped() throws Exception {
        MvcResult r = get("/actuator/health");

        assertThat(r.getResponse().getStatus()).isEqualTo(200);
        assertThat(JSON.readTree(r.getResponse().getContentAsString()).get("status").asText()).isEqualTo("UP");
    }

    // ------------------------------------------------------------------
    // 约定本身
    // ------------------------------------------------------------------

    @Test
    @DisplayName("成功码是 0；每个错误码都落在 4xx/5xx，且 {code,message,data} 里 code 等于 HTTP 状态码")
    void errorCodeTable() {
        assertThat(ApiResponse.SUCCESS_CODE).isZero();
        assertThat(ApiResponse.ok(null).message()).isEqualTo("success");

        // 每个 ErrorCode 的状态码都必须是合法的 HTTP 错误状态（多个错误码可以共用同一个状态码：
        // ILLEGAL_TRANSITION 与 DEPENDENCY_VIOLATION 都是 422，靠 message 区分）
        assertThat(Arrays.stream(ErrorCode.values()).map(ErrorCode::httpStatus))
            .as("全部错误码必须是合法的 HTTP 错误状态")
            .allMatch(s -> s >= 400 && s < 600);

        // ErrorCode → 状态码必须如实反映在 ApiResponse 里
        for (ErrorCode c : ErrorCode.values()) {
            ApiError err = ApiError.of(c, "x");
            assertThat(err.httpStatus()).isEqualTo(c.httpStatus());

            ApiResponse<Void> wrapped = ApiResponse.fail(err.httpStatus(), err.message());
            assertThat(wrapped.code()).isEqualTo(c.httpStatus());
            assertThat(wrapped.data()).isNull();
        }
    }
}
