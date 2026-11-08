package io.cadence.core.service;

public class TaskNotFoundException extends RuntimeException {
    public TaskNotFoundException(String id) {
        super("任务不存在: " + id);
    }
}
