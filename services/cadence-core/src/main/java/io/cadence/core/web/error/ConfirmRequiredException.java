package io.cadence.core.web.error;

/**
 * 破坏性操作缺少确认令牌。
 *
 * 这是 FR-4 的实现：删除必须先 dry-run 拿到令牌，再带令牌真正删除。
 * Agent 侧同样有 confirm 机制，这里是对直接调用方（curl / 其他客户端）的最后一道闸。
 */
public class ConfirmRequiredException extends RuntimeException {

    private final String confirmToken;
    private final Object snapshot;

    public ConfirmRequiredException(String message, String confirmToken, Object snapshot) {
        super(message);
        this.confirmToken = confirmToken;
        this.snapshot = snapshot;
    }

    public String getConfirmToken() { return confirmToken; }
    public Object getSnapshot() { return snapshot; }
}
