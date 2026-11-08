package io.cadence.core.web.error;

import java.util.List;
import java.util.Map;

/**
 * 批量更新中有条目版本冲突。
 * 整个批次回滚，返回 applied=0 —— 这正是和"循环调单条"的关键区别。
 */
public class BatchConflictException extends RuntimeException {

    private final List<Map<String, Object>> failedOps;

    public BatchConflictException(List<Map<String, Object>> failedOps) {
        super("批量更新中有 " + failedOps.size() + " 条版本冲突，整批已回滚");
        this.failedOps = failedOps;
    }

    public List<Map<String, Object>> getFailedOps() {
        return failedOps;
    }
}
