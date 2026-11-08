package io.cadence.core.web.error;

/**
 * 状态机不允许的流转。
 *
 * recoverable=false —— Agent 重试同样的流转永远不会成功，
 * 必须由用户改变意图。这是 422 而不是 409 的原因：
 * 409 意味着"再试一次可能有结果"，422 意味着"这个请求本身就不成立"。
 */
public class IllegalTransitionException extends RuntimeException {

    private final Object from;
    private final String action;

    public IllegalTransitionException(Object from, String action, String message) {
        super(message);
        this.from = from;
        this.action = action;
    }

    public Object getFrom() { return from; }
    public String getAction() { return action; }
}
