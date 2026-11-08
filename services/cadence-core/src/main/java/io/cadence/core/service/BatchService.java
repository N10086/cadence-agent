package io.cadence.core.service;

import io.cadence.core.domain.Task;
import io.cadence.core.repository.TaskRepository;
import io.cadence.core.web.dto.BatchUpdateRequest;
import io.cadence.core.web.error.BatchConflictException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 批量更新。
 *
 * 三条设计约束：
 *   1. 全成功或全回滚 —— 由 @Transactional 保证，任何一条失败整批撤销
 *   2. 逐条 version 校验 —— 允许调用方混合不同任务
 *   3. 结果报告每一条的最终状态 —— 即使失败也告诉调用方"哪些改了哪些没改"
 *
 * 为什么不能循环调单条 PATCH：
 *   会产生"前 5 个成功、第 6 个 409、后 3 个没动"的中间态。
 *   用户看到的是一个自相矛盾的数据库，Agent 也无法可靠回滚。
 */
@Service
public class BatchService {

    private final TaskRepository repo;

    public BatchService(TaskRepository repo) {
        this.repo = repo;
    }

    @Transactional
    public BatchResult apply(BatchUpdateRequest req) {
        Map<String, Task> loaded = new HashMap<>();
        for (BatchUpdateRequest.BatchOp op : req.ops()) {
            Task t = repo.findById(op.id())
                .orElseThrow(() -> new io.cadence.core.service.TaskNotFoundException(op.id()));
            if (t.isDeleted()) {
                throw new io.cadence.core.service.TaskNotFoundException(op.id());
            }
            loaded.put(op.id(), t);
        }

        // 先整体校验版本，任何一条不符立即失败（不写任何数据）
        List<Map<String, Object>> conflicts = new ArrayList<>();
        for (BatchUpdateRequest.BatchOp op : req.ops()) {
            Task t = loaded.get(op.id());
            if (!op.version().equals(t.getVersion())) {
                conflicts.add(Map.of(
                    "id", op.id(),
                    "expectedVersion", op.version(),
                    "actualVersion", t.getVersion()));
            }
        }
        if (!conflicts.isEmpty()) {
            throw new BatchConflictException(conflicts);
        }

        // 全部版本正确，再逐条应用
        List<Task> changed = new ArrayList<>();
        for (BatchUpdateRequest.BatchOp op : req.ops()) {
            Task t = loaded.get(op.id());
            BatchUpdateRequest.Patch p = op.patch();
            if (p != null) {
                if (p.title() != null)       t.setTitle(p.title().trim());
                if (p.description() != null) t.setDescription(p.description());
                if (p.priority() != null)    t.setPriority(p.priority());
                if (p.startAt() != null)     t.setStartAt(p.startAt());
                if (p.dueAt() != null)       t.setDueAt(p.dueAt());
                if (p.estimateMin() != null) t.setEstimateMin(p.estimateMin());
                if (p.pinned() != null)      t.setPinned(p.pinned());
                if (p.rrule() != null)       t.setRrule(p.rrule());
                if (p.tags() != null)        t.setTags(p.tags());
            }
            changed.add(t);
        }

        List<Task> saved = repo.saveAll(changed);
        return new BatchResult(saved.size(), saved.stream().map(Task::getId).toList());
    }

    public record BatchResult(int applied, List<String> ids) {}
}
