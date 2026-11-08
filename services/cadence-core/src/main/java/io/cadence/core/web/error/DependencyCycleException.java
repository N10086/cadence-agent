package io.cadence.core.web.error;

public class DependencyCycleException extends RuntimeException {
    public DependencyCycleException(String taskId, String dependsOnId) {
        super("添加依赖 " + taskId + " -> " + dependsOnId + " 会形成环");
    }
}
