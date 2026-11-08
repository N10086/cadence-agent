package io.cadence.core.web.error;

public class DependencyViolationException extends RuntimeException {
    public DependencyViolationException(String message) {
        super(message);
    }
}
