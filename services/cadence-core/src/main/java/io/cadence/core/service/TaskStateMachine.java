package io.cadence.core.service;

import io.cadence.core.domain.TaskStatus;
import io.cadence.core.web.error.IllegalTransitionException;
import org.springframework.stereotype.Component;

/**
 * 任务状态机。
 *
 * 允许的流转：
 *
 *        ┌──────────────────────────────────────┐
 *        │                                      │
 *        ▼                                      │
 *    ┌───────┐  start   ┌───────┐  complete  ┌──────┐
 *    │ TODO  │─────────►│ DOING │───────────►│ DONE │
 *    └───────┘          └───────┘            └──────┘
 *        │                  │                    │
 *        │ cancel           │ cancel             │ reopen
 *        ▼                  ▼                    ▼
 *    ┌───────────┐     ┌───────────┐         (back to TODO)
 *    │ CANCELLED │◄────│ CANCELLED │
 *    └───────────┘     └───────────┘
 *
 * 明确禁止：
 *   - DONE → DOING（必须先 reopen 回 TODO）
 *   - CANCELLED → DONE（必须先恢复）
 *   - 任何 → CANCELLED → 直接回 DOING/DONE
 *
 * 为什么把规则抽出来：这些约束会被 Agent 的 task.update 触及，
 * 散布在 service 里就没人能一眼看出全貌，也没法做穷举测试。
 */
@Component
public class TaskStateMachine {

    public void assertCanStart(TaskStatus from) {
        if (from == TaskStatus.DOING) {
            return;   // 幂等
        }
        if (from == TaskStatus.TODO) {
            return;
        }
        throw new IllegalTransitionException(from, "start",
            "只有 TODO 状态可以开始；当前是 " + from);
    }

    public void assertCanComplete(TaskStatus from) {
        if (from == TaskStatus.DONE) {
            return;   // 幂等，由调用方处理
        }
        if (from == TaskStatus.TODO || from == TaskStatus.DOING) {
            return;
        }
        throw new IllegalTransitionException(from, "complete",
            "已取消的任务不能标记完成，请先 reopen 恢复为 TODO");
    }

    public void assertCanReopen(TaskStatus from) {
        if (from == TaskStatus.TODO || from == TaskStatus.DOING) {
            return;   // 幂等
        }
        if (from == TaskStatus.DONE || from == TaskStatus.CANCELLED) {
            return;
        }
        throw new IllegalTransitionException(from, "reopen", "无法从 " + from + " 恢复");
    }

    public void assertCanCancel(TaskStatus from) {
        if (from == TaskStatus.CANCELLED) {
            return;   // 幂等
        }
        if (from == TaskStatus.DONE) {
            throw new IllegalTransitionException(from, "cancel",
                "已完成的任务不能取消。若要撤销请先 reopen 再取消");
        }
        return;
    }
}
