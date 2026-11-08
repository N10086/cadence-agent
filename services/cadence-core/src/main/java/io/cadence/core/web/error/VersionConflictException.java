package io.cadence.core.web.error;

/**
 * 乐观锁前置校验失败。
 * 与 ObjectOptimisticLockingFailureException 一起，统一映射为 409 CONFLICT。
 */
public class VersionConflictException extends RuntimeException {

    private final String taskId;
    private final Long expected;
    private final Long actual;

    public VersionConflictException(String taskId, Long expected, Long actual) {
        super("任务 " + taskId + " 的 version 不匹配：期望 " + expected + "，实际 " + actual);
        this.taskId = taskId;
        this.expected = expected;
        this.actual = actual;
    }

    public String getTaskId() { return taskId; }
    public Long getExpected() { return expected; }
    public Long getActual() { return actual; }
}
