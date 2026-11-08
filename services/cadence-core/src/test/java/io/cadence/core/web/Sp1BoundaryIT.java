package io.cadence.core.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import io.cadence.core.AbstractBoundaryIntegrationTest;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
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
 * SP1 HTTP 边界测试集。
 *
 * 目标不是重复现有 IT 的 happy path，而是把"参数边界、状态机边界、事务边界、
 * 时间边界、软删除边界、幂等边界、分页边界"逐条钉住。
 *
 * 约定：
 * - 断言规格行为的测试如果当前实现不满足，标 {@code @Disabled("BUG-SP1-xx ...")}；
 * - 同名 {@code bug_*} 用例锁定当前行为，作为修复前的可执行证据；修复后它会失败，
 *   提醒删除/改写。
 */
@AutoConfigureMockMvc
class Sp1BoundaryIT extends AbstractBoundaryIntegrationTest {

    private static final ObjectMapper JSON = new ObjectMapper();

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;

    @BeforeEach
    void clean() {
        jdbc.execute("TRUNCATE audit_log, idempotency_record, task_dependency, task CASCADE");
    }

    // ==================================================================
    // helpers
    // ==================================================================

    private static JsonNode body(MvcResult r) throws Exception {
        String s = r.getResponse().getContentAsString();
        return s.isBlank() ? JSON.readTree("{}") : JSON.readTree(s);
    }

    private JsonNode data(MvcResult r) throws Exception {
        JsonNode d = body(r).get("data");
        assertThat(d).as("响应缺少 data: " + r.getResponse().getContentAsString()).isNotNull();
        return d;
    }

    private String json(Object o) {
        try {
            return JSON.writeValueAsString(o);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }

    private MvcResult createRaw(Object body) throws Exception {
        return mvc.perform(post("/api/v1/tasks")
            .contentType(MediaType.APPLICATION_JSON).content(json(body))).andReturn();
    }

    private String create(String title, Map<String, Object> extra) throws Exception {
        var b = new LinkedHashMap<String, Object>();
        b.put("title", title);
        if (extra != null) b.putAll(extra);
        MvcResult r = createRaw(b);
        assertThat(r.getResponse().getStatus())
            .as("创建失败: " + r.getResponse().getContentAsString()).isEqualTo(201);
        return data(r).get("id").asText();
    }

    private String create(String title) throws Exception {
        return create(title, null);
    }

    private MvcResult state(String id, String action, Object version) throws Exception {
        return mvc.perform(post("/api/v1/tasks/" + id + ":" + action)
            .contentType(MediaType.APPLICATION_JSON)
            .content(version == null ? "{}" : json(Map.of("version", version)))).andReturn();
    }

    private MvcResult patchTask(String id, Object version, Map<String, Object> fields) throws Exception {
        var b = new LinkedHashMap<String, Object>();
        if (version != null) b.put("version", version);
        if (fields != null) b.putAll(fields);
        return mvc.perform(patch("/api/v1/tasks/" + id)
            .contentType(MediaType.APPLICATION_JSON).content(json(b))).andReturn();
    }

    private JsonNode getData(String path, String... params) throws Exception {
        var req = get(path);
        for (int i = 0; i + 1 < params.length; i += 2) {
            req.param(params[i], params[i + 1]);
        }
        MvcResult r = mvc.perform(req).andReturn();
        assertThat(r.getResponse().getStatus()).as(path).isEqualTo(200);
        return data(r);
    }

    private JsonNode list(String query) throws Exception {
        var req = get("/api/v1/tasks");
        if (query != null && !query.isBlank()) {
            for (String pair : query.split("&")) {
                int eq = pair.indexOf('=');
                if (eq > 0) {
                    // 测试里用 %2B 表示 '+'；MockMvc 的 param() 不做 URL 解码，这里手动还原
                    req.param(pair.substring(0, eq), pair.substring(eq + 1).replace("%2B", "+"));
                }
            }
        }
        MvcResult r = mvc.perform(req).andReturn();
        assertThat(r.getResponse().getStatus())
            .as("GET /api/v1/tasks?" + query + " -> " + r.getResponse().getContentAsString())
            .isEqualTo(200);
        return data(r);
    }

    private String deleteToken(String id) throws Exception {
        MvcResult r = mvc.perform(delete("/api/v1/tasks/" + id)).andReturn();
        assertThat(r.getResponse().getStatus()).isEqualTo(403);
        return body(r).get("data").get("confirmToken").asText();
    }

    private void deleteTask(String id) throws Exception {
        String token = deleteToken(id);
        MvcResult r = mvc.perform(delete("/api/v1/tasks/" + id)
            .header("X-Confirm-Token", token)).andReturn();
        assertThat(r.getResponse().getStatus()).isEqualTo(204);
    }

    // ==================================================================
    // 创建：字段边界
    // ==================================================================

    @Nested
    @DisplayName("创建任务：字段边界")
    class CreateFields {

        @Test
        @DisplayName("title 缺失 / 空串 / 全空格 / 超 200：全部 400")
        void titleBoundaries() throws Exception {
            assertThat(createRaw(Map.of()).getResponse().getStatus()).isEqualTo(400);
            assertThat(createRaw(Map.of("title", "")).getResponse().getStatus()).isEqualTo(400);
            assertThat(createRaw(Map.of("title", "   ")).getResponse().getStatus()).isEqualTo(400);
            assertThat(createRaw(Map.of("title", "x".repeat(201))).getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @DisplayName("title 恰好 200 字符：201")
        void titleExactly200() throws Exception {
            MvcResult r = createRaw(Map.of("title", "x".repeat(200)));
            assertThat(r.getResponse().getStatus()).isEqualTo(201);
            assertThat(data(r).get("title").asText()).hasSize(200);
        }

        @Test
        @DisplayName("estimateMin 的四个边界：4/481 拒绝，5/480 接受")
        void estimateBoundaries() throws Exception {
            assertThat(createRaw(Map.of("title", "e4", "estimateMin", 4)).getResponse().getStatus()).isEqualTo(400);
            assertThat(createRaw(Map.of("title", "e481", "estimateMin", 481)).getResponse().getStatus()).isEqualTo(400);
            assertThat(createRaw(Map.of("title", "e5", "estimateMin", 5)).getResponse().getStatus()).isEqualTo(201);
            assertThat(createRaw(Map.of("title", "e480", "estimateMin", 480)).getResponse().getStatus()).isEqualTo(201);
        }

        @Test
        @DisplayName("非法枚举 / 非法时间格式：400")
        void invalidEnumsAndDates() throws Exception {
            assertThat(createRaw(Map.of("title", "p", "priority", "SUPER")).getResponse().getStatus()).isEqualTo(400);
            assertThat(createRaw(Map.of("title", "d", "dueAt", "not-a-date")).getResponse().getStatus()).isEqualTo(400);
            assertThat(createRaw(Map.of("title", "n", "dueAt", "2026-11-05")).getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @DisplayName("未知字段被 Jackson 静默忽略（契约未声明 additionalProperties）")
        void unknownFieldIgnored() throws Exception {
            MvcResult r = createRaw(Map.of("title", "unknown-field", "foo", 1));
            assertThat(r.getResponse().getStatus()).isEqualTo(201);
            assertThat(data(r).has("foo")).isFalse();
        }

        @Test
        @DisplayName("版本号从 0 开始，响应必须带 version（Agent 依赖它做乐观锁）")
        void initialVersionIsZero() throws Exception {
            MvcResult r = createRaw(Map.of("title", "v0"));
            assertThat(data(r).get("version").asInt()).isZero();
        }

        @Test
        @Disabled("BUG-SP1-06：parentId 没有存在性校验，FK 违约冒到 500；应是 404/400")
        @DisplayName("parentId 不存在：应返回 4xx，而不是 500")
        void missingParentIsClientError() throws Exception {
            MvcResult r = createRaw(Map.of("title", "orphan", "parentId", UUID.randomUUID().toString()));
            assertThat(r.getResponse().getStatus()).isBetween(400, 499);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-06：parentId 不存在返回 500")
        void bug_missingParentReturns500() throws Exception {
            MvcResult r = createRaw(Map.of("title", "orphan", "parentId", UUID.randomUUID().toString()));
            assertThat(r.getResponse().getStatus()).isEqualTo(500);
        }

        @Test
        @Disabled("BUG-SP1-04：rrule 只在展开时解析，创建时非法规则也返回 201")
        @DisplayName("非法 rrule：创建时就应 400")
        void invalidRruleRejectedAtCreate() throws Exception {
            assertThat(createRaw(Map.of("title", "bad-rrule", "rrule", "NOT_A_RULE"))
                .getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-04：非法 rrule 创建成功，展开时才 400")
        void bug_invalidRruleAcceptedThenFailsOnExpand() throws Exception {
            MvcResult r = createRaw(Map.of("title", "bad-rrule",
                "startAt", "2026-11-01T09:00:00+08:00", "rrule", "NOT_A_RULE"));
            assertThat(r.getResponse().getStatus()).isEqualTo(201);
            String id = data(r).get("id").asText();
            MvcResult occ = mvc.perform(get("/api/v1/tasks/" + id + "/occurrences")
                .param("from", "2026-11-01T00:00:00+08:00")
                .param("to", "2026-12-01T00:00:00+08:00")).andReturn();
            assertThat(occ.getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @Disabled("BUG-SP1-05：startAt > dueAt 没有跨字段校验，创建成功")
        @DisplayName("startAt 晚于 dueAt：业务上应拒绝")
        void startAfterDueRejected() throws Exception {
            assertThat(createRaw(Map.of("title", "inverted",
                "startAt", "2026-11-10T09:00:00+08:00",
                "dueAt", "2026-11-09T09:00:00+08:00")).getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-05：startAt 晚于 dueAt 仍创建成功")
        void bug_startAfterDueAccepted() throws Exception {
            MvcResult r = createRaw(Map.of("title", "inverted",
                "startAt", "2026-11-10T09:00:00+08:00",
                "dueAt", "2026-11-09T09:00:00+08:00"));
            assertThat(r.getResponse().getStatus()).isEqualTo(201);
        }
    }

    // ==================================================================
    // 列表：过滤、排序、分页
    // ==================================================================

    @Nested
    @DisplayName("列表：过滤、排序、分页")
    class ListBoundaries {

        @Test
        @DisplayName("limit 越界被夹到 [1,50]，非数字 400")
        void limitClamp() throws Exception {
            create("a");
            create("b");
            create("c");
            assertThat(list("limit=0").get("items").size()).isEqualTo(1);
            assertThat(list("limit=-5").get("items").size()).isEqualTo(1);
            assertThat(list("limit=999").get("items").size()).isEqualTo(3);
            assertThat(mvc.perform(get("/api/v1/tasks").param("limit", "abc"))
                .andReturn().getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @DisplayName("hasMore 的精确边界：恰好 limit 条为 false，多一条为 true")
        void hasMoreExactBoundary() throws Exception {
            for (int i = 0; i < 20; i++) create("t" + i);
            JsonNode twenty = list("limit=20");
            assertThat(twenty.get("items").size()).isEqualTo(20);
            assertThat(twenty.get("hasMore").asBoolean()).isFalse();
            assertThat(twenty.get("total").asInt()).isEqualTo(20);

            create("t20");
            JsonNode twentyOne = list("limit=20");
            assertThat(twentyOne.get("items").size()).isEqualTo(20);
            assertThat(twentyOne.get("hasMore").asBoolean()).isTrue();
            assertThat(twentyOne.get("total").asInt()).isEqualTo(21);
        }

        @Test
        @DisplayName("排序：dueAt 升序、无 dueAt 排最后")
        void nullsLast() throws Exception {
            create("no-due");
            create("late", Map.of("dueAt", "2026-11-10T09:00:00+08:00"));
            create("early", Map.of("dueAt", "2026-11-09T09:00:00+08:00"));

            JsonNode items = list("limit=20").get("items");
            assertThat(items.get(0).get("title").asText()).isEqualTo("early");
            assertThat(items.get(1).get("title").asText()).isEqualTo("late");
            assertThat(items.get(2).get("title").asText()).isEqualTo("no-due");
        }

        @Test
        @DisplayName("keyword 只搜标题、忽略大小写；不搜 description（与工具描述不一致）")
        void keywordScope() throws Exception {
            create("Buy Milk", Map.of("description", "needle-in-description"));
            assertThat(list("keyword=BUY").get("total").asInt()).isEqualTo(1);
            assertThat(list("keyword=buy").get("total").asInt()).isEqualTo(1);
            assertThat(list("keyword=needle").get("total").asInt()).isZero();
        }

        @Test
        @DisplayName("dueBefore / dueAfter 是闭区间（= 边界值应命中）")
        void dueRangeInclusive() throws Exception {
            create("on", Map.of("dueAt", "2026-11-10T09:00:00+08:00"));
            create("after", Map.of("dueAt", "2026-11-11T09:00:00+08:00"));

            assertThat(list("dueBefore=2026-11-10T09:00:00%2B08:00").get("total").asInt()).isEqualTo(1);
            assertThat(list("dueAfter=2026-11-10T09:00:00%2B08:00").get("total").asInt()).isEqualTo(2);
        }

        @Test
        @DisplayName("status / priority 组合过滤是 AND")
        void combinedFilters() throws Exception {
            create("w1", Map.of("priority", "HIGH", "tags", List.of("work")));
            create("w2", Map.of("priority", "LOW", "tags", List.of("work")));
            create("h1", Map.of("priority", "HIGH", "tags", List.of("home")));

            assertThat(list("priority=HIGH").get("total").asInt()).isEqualTo(2);
            assertThat(list("priority=HIGH&status=TODO").get("total").asInt()).isEqualTo(2);
        }

        @Test
        @Disabled("BUG-SP1-57：TaskSpecifications.hasTag 生成 "
            + "case when tags is not null then coalesce(array_position(tags,?),0) end is not null，"
            + "coalesce 把 null 变成 0，条件恒真，tag 过滤完全失效")
        @DisplayName("tag 过滤：只返回含该标签的任务")
        void tagFilter() throws Exception {
            create("w1", Map.of("tags", List.of("work")));
            create("h1", Map.of("tags", List.of("home")));
            assertThat(list("tag=work").get("total").asInt()).isEqualTo(1);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-57：tag=work 返回全部任务")
        void bug_tagFilterIsNoop() throws Exception {
            create("w1", Map.of("tags", List.of("work")));
            create("h1", Map.of("tags", List.of("home")));
            assertThat(list("tag=work").get("total").asInt()).isEqualTo(2);
        }

        @Test
        @DisplayName("软删除任务不出现在列表")
        void deletedExcluded() throws Exception {
            String id = create("to-delete");
            deleteTask(id);
            assertThat(list(null).get("total").asInt()).isZero();
            assertThat(mvc.perform(get("/api/v1/tasks/" + id)).andReturn().getResponse().getStatus())
                .isEqualTo(404);
        }
    }

    // ==================================================================
    // PATCH：乐观锁与字段边界
    // ==================================================================

    @Nested
    @DisplayName("PATCH：乐观锁与字段边界")
    class PatchBoundaries {

        @Test
        @DisplayName("version 缺失 400；version 过期 409；正确 version 200 且 version+1")
        void versionBoundaries() throws Exception {
            String id = create("v");
            assertThat(patchTask(id, null, Map.of("title", "x")).getResponse().getStatus()).isEqualTo(400);

            assertThat(patchTask(id, 999, Map.of("title", "x")).getResponse().getStatus()).isEqualTo(409);
            assertThat(getData("/api/v1/tasks/" + id).get("task").get("version").asInt()).isZero();

            MvcResult ok = patchTask(id, 0, Map.of("title", "x"));
            assertThat(ok.getResponse().getStatus()).isEqualTo(200);
            assertThat(data(ok).get("task").get("version").asInt()).isEqualTo(1);
        }

        @Test
        @DisplayName("未出现的字段保持不变（null 表示不修改）")
        void nullMeansNoop() throws Exception {
            String id = create("keep", Map.of("priority", "HIGH", "estimateMin", 30,
                "description", "desc", "tags", List.of("a")));
            MvcResult r = patchTask(id, 0, Map.of("title", "changed"));
            JsonNode t = data(r).get("task");
            assertThat(t.get("title").asText()).isEqualTo("changed");
            assertThat(t.get("priority").asText()).isEqualTo("HIGH");
            assertThat(t.get("estimateMin").asInt()).isEqualTo(30);
            assertThat(t.get("description").asText()).isEqualTo("desc");
            assertThat(t.get("tags").size()).isEqualTo(1);
        }

        @Test
        @DisplayName("tags 是整体替换，不是追加")
        void tagsReplace() throws Exception {
            String id = create("tags", Map.of("tags", List.of("a", "b")));
            MvcResult r = patchTask(id, 0, Map.of("tags", List.of("c")));
            JsonNode tags = data(r).get("task").get("tags");
            assertThat(tags.size()).isEqualTo(1);
            assertThat(tags.get(0).asText()).isEqualTo("c");
        }

        @Test
        @Disabled("BUG-SP1-03：PATCH 的 title 没有 @NotBlank，空串/全空格能落库")
        @DisplayName("PATCH title 空串/全空格：应 400")
        void blankTitleRejected() throws Exception {
            String id = create("ok");
            assertThat(patchTask(id, 0, Map.of("title", "   ")).getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-03：PATCH title 全空格返回 200 且标题变成空")
        void bug_blankTitleAccepted() throws Exception {
            String id = create("ok");
            MvcResult r = patchTask(id, 0, Map.of("title", "   "));
            assertThat(r.getResponse().getStatus()).isEqualTo(200);
            assertThat(data(r).get("task").get("title").asText()).isEmpty();
        }

        @Test
        @DisplayName("description 超 4000：当前 PATCH 无长度校验，能写入（与 CREATE 不一致）")
        void descriptionLengthInconsistent() throws Exception {
            String id = create("d");
            MvcResult r = patchTask(id, 0, Map.of("description", "x".repeat(4001)));
            assertThat(r.getResponse().getStatus()).isEqualTo(200);
        }

        @Test
        @DisplayName("PATCH status 字段被忽略（DTO 不含 status），不会报错也不会改状态")
        void statusFieldIgnored() throws Exception {
            String id = create("s");
            MvcResult r = patchTask(id, 0, Map.of("status", "DONE"));
            assertThat(r.getResponse().getStatus()).isEqualTo(200);
            assertThat(data(r).get("task").get("status").asText()).isEqualTo("TODO");
        }
    }

    // ==================================================================
    // 状态机
    // ==================================================================

    @Nested
    @DisplayName("状态机边界")
    class StateMachineBoundaries {

        @Test
        @DisplayName("完整矩阵：合法流转 200，非法流转 422")
        void transitionMatrix() throws Exception {
            String todo = create("todo");
            assertThat(state(todo, "start", 0).getResponse().getStatus()).isEqualTo(200);
            assertThat(state(todo, "start", 999).getResponse().getStatus()).isEqualTo(200); // DOING 幂等
            assertThat(state(todo, "cancel", 1).getResponse().getStatus()).isEqualTo(200);
            assertThat(state(todo, "cancel", 999).getResponse().getStatus()).isEqualTo(200); // CANCELLED 幂等
            assertThat(state(todo, "complete", 2).getResponse().getStatus()).isEqualTo(422); // CANCELLED 不能完成
            assertThat(state(todo, "start", 2).getResponse().getStatus()).isEqualTo(422);
            assertThat(state(todo, "reopen", 2).getResponse().getStatus()).isEqualTo(200);
            assertThat(state(todo, "reopen", 999).getResponse().getStatus()).isEqualTo(200); // TODO 幂等

            String done = create("done");
            assertThat(state(done, "complete", 0).getResponse().getStatus()).isEqualTo(200);
            assertThat(state(done, "complete", 999).getResponse().getStatus()).isEqualTo(200); // DONE 幂等
            assertThat(state(done, "start", 1).getResponse().getStatus()).isEqualTo(422);
            assertThat(state(done, "cancel", 1).getResponse().getStatus()).isEqualTo(422);
        }

        @Test
        @Disabled("BUG-SP1-01：cancel 没有调用 assertVersionMatches，过期 version 也会成功")
        @DisplayName("cancel 使用过期 version：应 409")
        void cancelStaleVersionConflicts() throws Exception {
            String id = create("c");
            patchTask(id, 0, Map.of("title", "bumped"));
            assertThat(state(id, "cancel", 0).getResponse().getStatus()).isEqualTo(409);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-01：cancel 忽略 version，过期 version 仍 200")
        void bug_cancelIgnoresVersion() throws Exception {
            String id = create("c");
            patchTask(id, 0, Map.of("title", "bumped"));
            MvcResult r = state(id, "cancel", 0);
            assertThat(r.getResponse().getStatus()).isEqualTo(200);
            assertThat(data(r).get("status").asText()).isEqualTo("CANCELLED");
        }

        @Test
        @Disabled("BUG-SP1-02：assertCanReopen 允许 DOING→TODO，但接口文档只写了 DONE/CANCELLED→TODO")
        @DisplayName("reopen 在 DOING 上：应 422（文档未定义该流转）")
        void reopenFromDoingRejected() throws Exception {
            String id = create("r");
            state(id, "start", 0);
            assertThat(state(id, "reopen", 1).getResponse().getStatus()).isEqualTo(422);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-02：DOING 可以 reopen 回 TODO（未文档化）")
        void bug_reopenFromDoingAccepted() throws Exception {
            String id = create("r");
            state(id, "start", 0);
            MvcResult r = state(id, "reopen", 1);
            assertThat(r.getResponse().getStatus()).isEqualTo(200);
            assertThat(data(r).get("status").asText()).isEqualTo("TODO");
        }
    }

    // ==================================================================
    // 删除与确认令牌
    // ==================================================================

    @Nested
    @DisplayName("删除与确认令牌")
    class DeleteBoundaries {

        @Test
        @DisplayName("两阶段删除：403 带令牌和快照 → 204 → 再查 404")
        void twoPhaseDelete() throws Exception {
            String id = create("del");
            MvcResult first = mvc.perform(delete("/api/v1/tasks/" + id)).andReturn();
            assertThat(first.getResponse().getStatus()).isEqualTo(403);
            JsonNode d = body(first).get("data");
            assertThat(d.get("expiresInSeconds").asInt()).isEqualTo(300);
            assertThat(d.get("snapshot").get("id").asText()).isEqualTo(id);

            String token = d.get("confirmToken").asText();
            assertThat(mvc.perform(delete("/api/v1/tasks/" + id).header("X-Confirm-Token", token))
                .andReturn().getResponse().getStatus()).isEqualTo(204);
            assertThat(mvc.perform(get("/api/v1/tasks/" + id)).andReturn().getResponse().getStatus())
                .isEqualTo(404);
        }

        @Test
        @DisplayName("令牌只属于签发它的任务：拿 A 的令牌删 B → 403，且 A/B 都还在")
        void tokenIsBoundToTask() throws Exception {
            String a = create("a");
            String b = create("b");
            String tokenA = deleteToken(a);
            assertThat(mvc.perform(delete("/api/v1/tasks/" + b).header("X-Confirm-Token", tokenA))
                .andReturn().getResponse().getStatus()).isEqualTo(403);
            assertThat(mvc.perform(get("/api/v1/tasks/" + a)).andReturn().getResponse().getStatus()).isEqualTo(200);
            assertThat(mvc.perform(get("/api/v1/tasks/" + b)).andReturn().getResponse().getStatus()).isEqualTo(200);
        }

        @Test
        @DisplayName("令牌单次使用：第一次 204，第二次 404（任务已删除）")
        void tokenSingleUse() throws Exception {
            String id = create("single");
            String token = deleteToken(id);
            assertThat(mvc.perform(delete("/api/v1/tasks/" + id).header("X-Confirm-Token", token))
                .andReturn().getResponse().getStatus()).isEqualTo(204);
            assertThat(mvc.perform(delete("/api/v1/tasks/" + id).header("X-Confirm-Token", token))
                .andReturn().getResponse().getStatus()).isEqualTo(404);
        }

        @Test
        @DisplayName("删除不要求 version（当前设计如此，文档 §9.2 #3 明示）")
        void deleteDoesNotRequireVersion() throws Exception {
            String id = create("noversion");
            patchTask(id, 0, Map.of("title", "bumped"));
            String token = deleteToken(id);
            assertThat(mvc.perform(delete("/api/v1/tasks/" + id).header("X-Confirm-Token", token))
                .andReturn().getResponse().getStatus()).isEqualTo(204);
        }

        @Test
        @DisplayName("不存在的任务：403 还是 404？（当前先 getOrThrow → 404）")
        void deleteMissingTask() throws Exception {
            assertThat(mvc.perform(delete("/api/v1/tasks/" + UUID.randomUUID()))
                .andReturn().getResponse().getStatus()).isEqualTo(404);
        }
    }

    // ==================================================================
    // 依赖
    // ==================================================================

    @Nested
    @DisplayName("依赖边界")
    class DependencyBoundaries {

        private MvcResult addDep(String task, String dep) throws Exception {
            return mvc.perform(post("/api/v1/tasks/" + task + "/dependencies")
                .contentType(MediaType.APPLICATION_JSON)
                .content(json(Map.of("dependsOnId", dep)))).andReturn();
        }

        @Test
        @DisplayName("自依赖 422；前置不存在 404；重复添加 201 且幂等")
        void addDependencyEdges() throws Exception {
            String a = create("a");
            String b = create("b");
            assertThat(addDep(a, a).getResponse().getStatus()).isEqualTo(422);
            assertThat(addDep(a, UUID.randomUUID().toString()).getResponse().getStatus()).isEqualTo(404);
            assertThat(addDep(a, b).getResponse().getStatus()).isEqualTo(201);
            assertThat(addDep(a, b).getResponse().getStatus()).isEqualTo(201);
            assertThat(getData("/api/v1/tasks/" + a + "/dependencies").size()).isEqualTo(1);
        }

        @Test
        @DisplayName("环检测：A→B、B→C 后 C→A 必须 422，且无新边")
        void cycleDetection() throws Exception {
            String a = create("a");
            String b = create("b");
            String c = create("c");
            assertThat(addDep(a, b).getResponse().getStatus()).isEqualTo(201);
            assertThat(addDep(b, c).getResponse().getStatus()).isEqualTo(201);
            assertThat(addDep(c, a).getResponse().getStatus()).isEqualTo(422);
            assertThat(getData("/api/v1/tasks/" + c + "/dependencies").size()).isZero();
        }

        @Test
        @DisplayName("删除依赖：存在 204；本来不存在也 204（幂等）")
        void removeDependency() throws Exception {
            String a = create("a");
            String b = create("b");
            addDep(a, b);
            assertThat(mvc.perform(delete("/api/v1/tasks/" + a + "/dependencies/" + b))
                .andReturn().getResponse().getStatus()).isEqualTo(204);
            assertThat(mvc.perform(delete("/api/v1/tasks/" + a + "/dependencies/" + b))
                .andReturn().getResponse().getStatus()).isEqualTo(204);
        }

        @Test
        @DisplayName("不存在的任务查依赖：200 + []（与详情 404 不一致，但文档明示）")
        void missingTaskDependencies() throws Exception {
            assertThat(getData("/api/v1/tasks/" + UUID.randomUUID() + "/dependencies").size()).isZero();
        }

        @Test
        @DisplayName("前置未完成时完成后继：422，message 带未完成 id")
        void incompletePrerequisiteBlocksComplete() throws Exception {
            String pre = create("pre");
            String post = create("post");
            addDep(post, pre);
            MvcResult r = state(post, "complete", 0);
            assertThat(r.getResponse().getStatus()).isEqualTo(422);
            assertThat(body(r).get("message").asText()).contains(pre);
        }

        @Test
        @DisplayName("前置完成后，后继可完成")
        void completedPrerequisiteAllowsComplete() throws Exception {
            String pre = create("pre");
            String post = create("post");
            addDep(post, pre);
            assertThat(state(pre, "complete", 0).getResponse().getStatus()).isEqualTo(200);
            assertThat(state(post, "complete", 0).getResponse().getStatus()).isEqualTo(200);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-23：前置被软删除后，后继永久无法完成（注释说'不阻止'，实现却计为未完成）")
        void bug_deletedPrerequisitePermanentlyBlocks() throws Exception {
            String pre = create("pre");
            String post = create("post");
            addDep(post, pre);
            deleteTask(pre);
            MvcResult r = state(post, "complete", 0);
            assertThat(r.getResponse().getStatus()).isEqualTo(422);
            assertThat(body(r).get("message").asText()).contains(pre);
        }
    }

    // ==================================================================
    // 批量
    // ==================================================================

    @Nested
    @DisplayName("批量更新：事务与边界")
    class BatchBoundaries {

        private MvcResult batch(List<Map<String, Object>> ops) throws Exception {
            return mvc.perform(post("/api/v1/tasks:batch")
                .contentType(MediaType.APPLICATION_JSON)
                .content(json(Map.of("ops", ops)))).andReturn();
        }

        private Map<String, Object> op(String id, long version, Map<String, Object> patch) {
            var m = new LinkedHashMap<String, Object>();
            m.put("id", id);
            m.put("version", version);
            m.put("patch", patch);
            return m;
        }

        @Test
        @DisplayName("空 ops：400")
        void emptyOps() throws Exception {
            assertThat(batch(List.of()).getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @DisplayName("全部成功：200 applied=2 ids 两条")
        void allSucceed() throws Exception {
            String a = create("a");
            String b = create("b");
            MvcResult r = batch(List.of(
                op(a, 0, Map.of("title", "a2")),
                op(b, 0, Map.of("priority", "HIGH"))));
            assertThat(r.getResponse().getStatus()).isEqualTo(200);
            assertThat(data(r).get("applied").asInt()).isEqualTo(2);
            assertThat(data(r).get("ids").size()).isEqualTo(2);
        }

        @Test
        @DisplayName("任一 version 过期：409，且整批一条都没改")
        void staleRollsBackAll() throws Exception {
            String a = create("a");
            String b = create("b");
            MvcResult r = batch(List.of(
                op(a, 0, Map.of("title", "a2")),
                op(b, 999, Map.of("title", "b2"))));
            assertThat(r.getResponse().getStatus()).isEqualTo(409);
            assertThat(getData("/api/v1/tasks/" + a).get("task").get("title").asText()).isEqualTo("a");
            assertThat(getData("/api/v1/tasks/" + b).get("task").get("title").asText()).isEqualTo("b");
        }

        @Test
        @DisplayName("任一任务不存在：404，且整批回滚")
        void missingRollsBackAll() throws Exception {
            String a = create("a");
            MvcResult r = batch(List.of(
                op(a, 0, Map.of("title", "a2")),
                op(UUID.randomUUID().toString(), 0, Map.of("title", "ghost"))));
            assertThat(r.getResponse().getStatus()).isEqualTo(404);
            assertThat(getData("/api/v1/tasks/" + a).get("task").get("title").asText()).isEqualTo("a");
        }

        @Test
        @DisplayName("batch 的 patch 没有 @NotBlank：空标题能写入")
        void blankTitleAccepted() throws Exception {
            String a = create("a");
            MvcResult r = batch(List.of(op(a, 0, Map.of("title", ""))));
            assertThat(r.getResponse().getStatus()).isEqualTo(200);
            assertThat(getData("/api/v1/tasks/" + a).get("task").get("title").asText()).isEmpty();
        }

        @Test
        @Disabled("BUG-SP1-17：batch patch 的 estimateMin 没有 @Min/@Max，越界值直到 DB CHECK 才炸，"
            + "对外表现为 500；应是 400")
        @DisplayName("batch patch estimateMin=9999：应 400")
        void estimateOutOfRangeIsClientError() throws Exception {
            String a = create("a");
            assertThat(batch(List.of(op(a, 0, Map.of("estimateMin", 9999))))
                .getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-17：batch estimateMin=9999 返回 500")
        void bug_batchEstimateOutOfRangeReturns500() throws Exception {
            String a = create("a");
            assertThat(batch(List.of(op(a, 0, Map.of("estimateMin", 9999))))
                .getResponse().getStatus()).isEqualTo(500);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-17：同一个 id 在同一批里出现两次会被应用两次/计数两次")
        void bug_duplicateIdsInSameBatch() throws Exception {
            String a = create("a");
            MvcResult r = batch(List.of(
                op(a, 0, Map.of("title", "first")),
                op(a, 0, Map.of("title", "second"))));
            assertThat(r.getResponse().getStatus()).isEqualTo(200);
            // 无论 applied 是 1 还是 2，最终只剩最后一次写入，且没有拒绝重复 id
            assertThat(data(r).get("applied").asInt()).isGreaterThanOrEqualTo(1);
            assertThat(getData("/api/v1/tasks/" + a).get("task").get("title").asText()).isEqualTo("second");
        }

        @Test
        @DisplayName("锁定 BUG-SP1-17：批量更新不写审计（文档 §9.2 #1 已知问题）")
        void bug_batchDoesNotAudit() throws Exception {
            String a = create("a");
            batch(List.of(op(a, 0, Map.of("title", "a2"))));
            JsonNode history = getData("/api/v1/tasks/" + a + "/history");
            List<String> actions = new ArrayList<>();
            history.forEach(n -> actions.add(n.get("action").asText()));
            assertThat(actions).doesNotContain("BATCH_UPDATE");
        }
    }

    // ==================================================================
    // 忙闲
    // ==================================================================

    @Nested
    @DisplayName("忙闲查询边界")
    class BusyBoundaries {

        private JsonNode busy(String from, String to) throws Exception {
            return getData("/api/v1/calendar/busy", "from", from, "to", to);
        }

        private String createTask(String title, String start, int estimate) throws Exception {
            return create(title, Map.of("startAt", start, "estimateMin", estimate));
        }

        @Test
        @DisplayName("from >= to：400（TimeWindow 构造拒绝）")
        void invertedRange() throws Exception {
            // 有任务时才会走到 TimeWindow.of(from,to) 的校验
            createTask("x", "2026-11-04T09:00:00+08:00", 60);
            assertThat(mvc.perform(get("/api/v1/calendar/busy")
                .param("from", "2026-11-05T00:00:00+08:00")
                .param("to", "2026-11-04T00:00:00+08:00"))
                .andReturn().getResponse().getStatus()).isEqualTo(400);
            assertThat(mvc.perform(get("/api/v1/calendar/busy")
                .param("from", "2026-11-04T00:00:00+08:00")
                .param("to", "2026-11-04T00:00:00+08:00"))
                .andReturn().getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-59：空日历时 from>=to 不校验，直接 200+[]")
        void bug_emptyCalendarInvertedRangeReturns200() throws Exception {
            assertThat(mvc.perform(get("/api/v1/calendar/busy")
                .param("from", "2026-11-05T00:00:00+08:00")
                .param("to", "2026-11-04T00:00:00+08:00"))
                .andReturn().getResponse().getStatus()).isEqualTo(200);
        }

        @Test
        @DisplayName("重叠/相接区间合并，minutes 是合并后时长")
        void mergeAdjacent() throws Exception {
            createTask("a", "2026-11-04T09:00:00+08:00", 60);
            createTask("b", "2026-11-04T10:00:00+08:00", 60);
            JsonNode windows = busy("2026-11-04T00:00:00+08:00", "2026-11-05T00:00:00+08:00")
                .get("windows");
            assertThat(windows.size()).isEqualTo(1);
            assertThat(windows.get(0).get("minutes").asInt()).isEqualTo(120);
        }

        @Test
        @DisplayName("返回区间被裁剪到 [from,to]")
        void clipToQueryRange() throws Exception {
            createTask("long", "2026-11-04T08:00:00+08:00", 480); // 08:00-16:00
            JsonNode windows = busy("2026-11-04T09:00:00+08:00", "2026-11-04T10:00:00+08:00")
                .get("windows");
            assertThat(windows.size()).isEqualTo(1);
            assertThat(windows.get(0).get("start").asText()).startsWith("2026-11-04T09:00:00");
            assertThat(windows.get(0).get("end").asText()).startsWith("2026-11-04T10:00:00");
            assertThat(windows.get(0).get("minutes").asInt()).isEqualTo(60);
        }

        @Test
        @DisplayName("DONE / CANCELLED 任务不占用时间")
        void finishedTasksExcluded() throws Exception {
            String done = createTask("done", "2026-11-04T09:00:00+08:00", 60);
            state(done, "complete", 0);
            String cancelled = createTask("cancelled", "2026-11-04T10:00:00+08:00", 60);
            state(cancelled, "cancel", 0);
            assertThat(busy("2026-11-04T00:00:00+08:00", "2026-11-05T00:00:00+08:00")
                .get("windows").size()).isZero();
        }

        @Test
        @DisplayName("没有 estimate 的任务不参与占用计算")
        void noEstimateIgnored() throws Exception {
            create("no-estimate", Map.of("startAt", "2026-11-04T09:00:00+08:00"));
            assertThat(busy("2026-11-04T00:00:00+08:00", "2026-11-05T00:00:00+08:00")
                .get("windows").size()).isZero();
        }
    }

    // ==================================================================
    // 重复任务展开
    // ==================================================================

    @Nested
    @DisplayName("重复任务展开边界")
    class OccurrenceBoundaries {

        private JsonNode occurrences(String id, String from, String to, String cap) throws Exception {
            var req = get("/api/v1/tasks/" + id + "/occurrences")
                .param("from", from).param("to", to);
            if (cap != null) req.param("cap", cap);
            MvcResult r = mvc.perform(req).andReturn();
            assertThat(r.getResponse().getStatus())
                .as("occurrences -> " + r.getResponse().getContentAsString()).isEqualTo(200);
            return data(r);
        }

        @Test
        @DisplayName("非重复任务：锚点在区间内返回 1 条；区间外返回 0 条")
        void nonRecurringAnchor() throws Exception {
            String id = create("single", Map.of("startAt", "2026-11-05T09:00:00+08:00"));
            assertThat(occurrences(id, "2026-11-01T00:00:00+08:00", "2026-11-30T00:00:00+08:00", null)
                .get("occurrences").size()).isEqualTo(1);
            assertThat(occurrences(id, "2026-12-01T00:00:00+08:00", "2026-12-31T00:00:00+08:00", null)
                .get("occurrences").size()).isZero();
        }

        @Test
        @DisplayName("非重复任务的 rrule 字段当前输出 null（文档说'不返回该字段'）")
        void nonRecurringRruleIsNull() throws Exception {
            String id = create("single", Map.of("startAt", "2026-11-05T09:00:00+08:00"));
            JsonNode d = occurrences(id, "2026-11-01T00:00:00+08:00", "2026-11-30T00:00:00+08:00", null);
            assertThat(d.has("rrule")).isTrue();
            assertThat(d.get("rrule").isNull()).isTrue();
        }

        @Test
        @DisplayName("cap 夹到 [1,500]：cap=0 仍返回 1 条，cap=999 最多 500 条")
        void capClamp() throws Exception {
            String id = create("daily", Map.of(
                "startAt", "2026-01-01T09:00:00+08:00",
                "rrule", "FREQ=DAILY"));
            assertThat(occurrences(id, "2026-11-01T00:00:00+08:00", "2026-11-30T00:00:00+08:00", "0")
                .get("occurrences").size()).isEqualTo(1);
            assertThat(occurrences(id, "2026-11-01T00:00:00+08:00", "2026-11-30T00:00:00+08:00", "999")
                .get("occurrences").size()).isEqualTo(29);
        }

        @Test
        @Disabled("BUG-SP1-09：truncated 用 size >= cap 判断，恰好 cap 条时会误报截断")
        @DisplayName("恰好 cap 条且没有更多：truncated 应为 false")
        void truncatedFalseWhenExactCap() throws Exception {
            String id = create("three", Map.of(
                "startAt", "2026-11-01T09:00:00+08:00",
                "rrule", "FREQ=DAILY;COUNT=3"));
            JsonNode d = occurrences(id, "2026-11-01T00:00:00+08:00", "2026-11-30T00:00:00+08:00", "3");
            assertThat(d.get("occurrences").size()).isEqualTo(3);
            assertThat(d.get("truncated").asBoolean()).isFalse();
        }

        @Test
        @DisplayName("锁定 BUG-SP1-09：恰好 3 条、cap=3 时 truncated=true")
        void bug_truncatedTrueAtExactCap() throws Exception {
            String id = create("three", Map.of(
                "startAt", "2026-11-01T09:00:00+08:00",
                "rrule", "FREQ=DAILY;COUNT=3"));
            JsonNode d = occurrences(id, "2026-11-01T00:00:00+08:00", "2026-11-30T00:00:00+08:00", "3");
            assertThat(d.get("truncated").asBoolean()).isTrue();
        }

        @Test
        @DisplayName("锁定 BUG-SP1-10：2000 年开始的每日重复，查询 2026 年返回空")
        void bug_ancientDailyReturnsEmpty() throws Exception {
            String id = create("ancient", Map.of(
                "startAt", "2000-01-01T09:00:00+08:00",
                "rrule", "FREQ=DAILY"));
            JsonNode d = occurrences(id, "2026-11-01T00:00:00+08:00", "2026-11-07T00:00:00+08:00", null);
            assertThat(d.get("occurrences").size()).isZero();
        }

        @Test
        @DisplayName("from > to：400")
        void invertedRangeIs400() throws Exception {
            String id = create("r", Map.of("startAt", "2026-11-05T09:00:00+08:00", "rrule", "FREQ=DAILY"));
            assertThat(mvc.perform(get("/api/v1/tasks/" + id + "/occurrences")
                .param("from", "2026-11-30T00:00:00+08:00")
                .param("to", "2026-11-01T00:00:00+08:00"))
                .andReturn().getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @Disabled("BUG-SP1-58：缺少必填 query 参数抛 MissingServletRequestParameterException，"
            + "被兜底 Exception handler 变成 500；应是 400")
        @DisplayName("缺少 from/to：应 400")
        void missingRequiredParamIs400() throws Exception {
            String id = create("r", Map.of("startAt", "2026-11-05T09:00:00+08:00", "rrule", "FREQ=DAILY"));
            assertThat(mvc.perform(get("/api/v1/tasks/" + id + "/occurrences"))
                .andReturn().getResponse().getStatus()).isEqualTo(400);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-58：缺少 from/to 返回 500")
        void bug_missingRequiredParamReturns500() throws Exception {
            String id = create("r", Map.of("startAt", "2026-11-05T09:00:00+08:00", "rrule", "FREQ=DAILY"));
            assertThat(mvc.perform(get("/api/v1/tasks/" + id + "/occurrences"))
                .andReturn().getResponse().getStatus()).isEqualTo(500);
        }

        @Test
        @DisplayName("不存在的任务：404")
        void missingTask() throws Exception {
            assertThat(mvc.perform(get("/api/v1/tasks/" + UUID.randomUUID() + "/occurrences")
                .param("from", "2026-11-01T00:00:00+08:00")
                .param("to", "2026-12-01T00:00:00+08:00"))
                .andReturn().getResponse().getStatus()).isEqualTo(404);
        }
    }

    // ==================================================================
    // 历史 / 审计
    // ==================================================================

    @Nested
    @DisplayName("历史与审计")
    class HistoryBoundaries {

        @Test
        @DisplayName("CREATE/UPDATE/COMPLETE 都进历史，按时间倒序，before/after 是 JSON 字符串")
        void historyActionsAndShape() throws Exception {
            String id = create("h");
            Thread.sleep(5);
            patchTask(id, 0, Map.of("title", "h2"));
            Thread.sleep(5);
            state(id, "complete", 1);

            JsonNode h = getData("/api/v1/tasks/" + id + "/history");
            assertThat(h.size()).isEqualTo(3);
            assertThat(h.get(0).get("action").asText()).isEqualTo("COMPLETE");
            assertThat(h.get(1).get("action").asText()).isEqualTo("UPDATE");
            assertThat(h.get(2).get("action").asText()).isEqualTo("CREATE");
            assertThat(h.get(1).get("before").isString()).isTrue();
            assertThat(h.get(1).get("after").isString()).isTrue();
            assertThat(h.get(0).get("after").asText()).contains("\"status\": \"DONE\"");
        }

        @Test
        @DisplayName("锁定 BUG-SP1-07：不存在的任务查历史返回 200 + []，文档要求 404")
        void bug_missingTaskHistoryReturns200() throws Exception {
            MvcResult r = mvc.perform(get("/api/v1/tasks/" + UUID.randomUUID() + "/history")).andReturn();
            assertThat(r.getResponse().getStatus()).isEqualTo(200);
            assertThat(data(r).size()).isZero();
        }

        @Test
        @DisplayName("锁定 BUG-SP1-07：已删除任务仍能查到历史（其他接口一律 404）")
        void bug_deletedTaskHistoryReturns200() throws Exception {
            String id = create("hdel");
            deleteTask(id);
            MvcResult r = mvc.perform(get("/api/v1/tasks/" + id + "/history")).andReturn();
            assertThat(r.getResponse().getStatus()).isEqualTo(200);
            assertThat(data(r).size()).isGreaterThanOrEqualTo(1);
        }
    }

    // ==================================================================
    // 幂等
    // ==================================================================

    @Nested
    @DisplayName("幂等创建")
    class IdempotencyBoundaries {

        @Test
        @DisplayName("同 key 同 body：返回同 id + Idempotent-Replayed:true，只落一条")
        void replay() throws Exception {
            String key = "k-" + UUID.randomUUID();
            MvcResult r1 = mvc.perform(post("/api/v1/tasks")
                .header("X-Idempotency-Key", key)
                .contentType(MediaType.APPLICATION_JSON)
                .content(json(Map.of("title", "idem")))).andReturn();
            MvcResult r2 = mvc.perform(post("/api/v1/tasks")
                .header("X-Idempotency-Key", key)
                .contentType(MediaType.APPLICATION_JSON)
                .content(json(Map.of("title", "idem")))).andReturn();

            assertThat(data(r2).get("id").asText()).isEqualTo(data(r1).get("id").asText());
            assertThat(r2.getResponse().getHeader("Idempotent-Replayed")).isEqualTo("true");
            assertThat(list(null).get("total").asInt()).isEqualTo(1);
        }

        @Test
        @DisplayName("同 key 不同 body：返回第一次的结果（幂等键语义，不校验 body）")
        void sameKeyDifferentBodyReturnsFirst() throws Exception {
            String key = "k-" + UUID.randomUUID();
            mvc.perform(post("/api/v1/tasks")
                .header("X-Idempotency-Key", key)
                .contentType(MediaType.APPLICATION_JSON)
                .content(json(Map.of("title", "first"))));
            MvcResult r2 = mvc.perform(post("/api/v1/tasks")
                .header("X-Idempotency-Key", key)
                .contentType(MediaType.APPLICATION_JSON)
                .content(json(Map.of("title", "second")))).andReturn();
            assertThat(data(r2).get("title").asText()).isEqualTo("first");
            assertThat(list(null).get("total").asInt()).isEqualTo(1);
        }
    }

    // ==================================================================
    // 冲突检测
    // ==================================================================

    @Nested
    @DisplayName("冲突检测")
    class ConflictBoundaries {

        @Test
        @DisplayName("锁定 BUG-SP1-08：overlapStart/End 返回的是对方任务的完整窗口，不是交集；"
            + "且未按 Asia/Shanghai 归一化（输出 Z）")
        void bug_overlapWindowIsNotIntersection() throws Exception {
            create("long", Map.of("startAt", "2026-11-20T09:00:00+08:00", "estimateMin", 480));
            String b = create("short", Map.of("startAt", "2026-11-20T15:00:00+08:00", "estimateMin", 30));

            JsonNode conflict = getData("/api/v1/tasks/" + b).get("conflicts").get(0);
            assertThat(conflict.get("overlapMinutes").asInt()).isEqualTo(30);
            // 期望交集 15:00-15:30（且带 +08:00）；当前实现给的是对方窗口 09:00-17:00，序列化成 UTC
            assertThat(conflict.get("overlapStart").asText()).startsWith("2026-11-20T01:00:00");
            assertThat(conflict.get("overlapEnd").asText()).startsWith("2026-11-20T09:00:00");
        }

        @Test
        @Disabled("BUG-SP1-08：overlapStart/End 应是交集而不是对方完整窗口，且应带 +08:00")
        @DisplayName("冲突窗口应等于交集")
        void overlapWindowIsIntersection() throws Exception {
            create("long", Map.of("startAt", "2026-11-20T09:00:00+08:00", "estimateMin", 480));
            String b = create("short", Map.of("startAt", "2026-11-20T15:00:00+08:00", "estimateMin", 30));

            JsonNode conflict = getData("/api/v1/tasks/" + b).get("conflicts").get(0);
            assertThat(conflict.get("overlapStart").asText()).isEqualTo("2026-11-20T15:00:00+08:00");
            assertThat(conflict.get("overlapEnd").asText()).isEqualTo("2026-11-20T15:30:00+08:00");
        }

        @Test
        @DisplayName("不相接、不重叠：conflicts 为空")
        void noConflictWhenTouching() throws Exception {
            create("a", Map.of("startAt", "2026-11-20T09:00:00+08:00", "estimateMin", 60));
            String b = create("b", Map.of("startAt", "2026-11-20T10:00:00+08:00", "estimateMin", 60));
            assertThat(getData("/api/v1/tasks/" + b).get("conflicts").size()).isZero();
        }
    }

    // ==================================================================
    // 协议外壳
    // ==================================================================

    @Nested
    @DisplayName("协议外壳")
    class EnvelopeBoundaries {

        @Test
        @DisplayName("路径不存在：404 + code=404")
        void notFound() throws Exception {
            MvcResult r = mvc.perform(get("/api/v1/nope")).andReturn();
            assertThat(r.getResponse().getStatus()).isEqualTo(404);
            assertThat(body(r).get("code").asInt()).isEqualTo(404);
        }

        @Test
        @DisplayName("方法不允许：405，且 code=405")
        void methodNotAllowed() throws Exception {
            MvcResult r = mvc.perform(post("/api/v1/calendar/busy")).andReturn();
            assertThat(r.getResponse().getStatus()).isEqualTo(405);
            assertThat(body(r).get("code").asInt()).isEqualTo(405);
        }

        @Test
        @Disabled("BUG-SP1-28：HttpMediaTypeNotSupportedException 没被专门处理，415 被兜底成 500")
        @DisplayName("Content-Type: text/plain：应 415")
        void unsupportedMediaTypeIs415() throws Exception {
            MvcResult r = mvc.perform(post("/api/v1/tasks")
                .contentType(MediaType.TEXT_PLAIN).content("{\"title\":\"x\"}"))
                .andReturn();
            assertThat(r.getResponse().getStatus()).isEqualTo(415);
        }

        @Test
        @DisplayName("锁定 BUG-SP1-28：text/plain 返回 500")
        void bug_unsupportedMediaTypeIs500() throws Exception {
            MvcResult r = mvc.perform(post("/api/v1/tasks")
                .contentType(MediaType.TEXT_PLAIN).content("{\"title\":\"x\"}"))
                .andReturn();
            assertThat(r.getResponse().getStatus()).isEqualTo(500);
        }

        @Test
        @DisplayName("204 无响应体")
        void noContentHasNoBody() throws Exception {
            String id = create("n");
            String token = deleteToken(id);
            MvcResult r = mvc.perform(delete("/api/v1/tasks/" + id)
                .header("X-Confirm-Token", token)).andReturn();
            assertThat(r.getResponse().getStatus()).isEqualTo(204);
            assertThat(r.getResponse().getContentAsString()).isEmpty();
        }
    }
}
