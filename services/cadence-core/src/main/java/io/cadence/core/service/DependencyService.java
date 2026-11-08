package io.cadence.core.service;

import io.cadence.core.domain.Task;
import io.cadence.core.domain.TaskDependency;
import io.cadence.core.domain.TaskStatus;
import io.cadence.core.repository.TaskDependencyRepository;
import io.cadence.core.repository.TaskRepository;
import io.cadence.core.web.error.DependencyCycleException;
import io.cadence.core.web.error.DependencyViolationException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 任务依赖。
 *
 * 两条硬约束：
 *   1. 不能成环 —— 加依赖前做可达性检查
 *   2. 前置未完成时后继不能 DONE —— 完成任务前检查
 *
 * 环检测用迭代式 DFS，不用递归：依赖图可能很深，
 * 递归在 1000 层左右就栈溢出了，而 DFS 用显式栈没有这个上限。
 */
@Service
public class DependencyService {

    private final TaskRepository taskRepo;
    private final TaskDependencyRepository depRepo;

    public DependencyService(TaskRepository taskRepo, TaskDependencyRepository depRepo) {
        this.taskRepo = taskRepo;
        this.depRepo = depRepo;
    }

    /**
     * 给 taskId 添加前置 dependsOnId。
     *
     * @throws DependencyCycleException 若会成环
     */
    @Transactional
    public void addDependency(String taskId, String dependsOnId) {
        if (taskId.equals(dependsOnId)) {
            throw new DependencyViolationException("任务不能依赖自己: " + taskId);
        }

        // 两端都必须存在且未删除
        requireLive(taskId);
        requireLive(dependsOnId);

        // 若已经存在，幂等返回
        var key = new TaskDependency.Key(taskId, dependsOnId);
        if (depRepo.existsById(key)) {
            return;
        }

        // 环检测：如果 dependsOnId 已经（直接或间接）依赖 taskId，再加就成环
        if (reaches(dependsOnId, taskId)) {
            throw new DependencyCycleException(taskId, dependsOnId);
        }

        depRepo.save(new TaskDependency(taskId, dependsOnId));
    }

    @Transactional
    public void removeDependency(String taskId, String dependsOnId) {
        depRepo.deleteById(new TaskDependency.Key(taskId, dependsOnId));
    }

    /** 我的前置（我依赖谁） */
    @Transactional(readOnly = true)
    public List<String> prerequisitesOf(String taskId) {
        return depRepo.findByIdTaskId(taskId).stream()
            .map(TaskDependency::getDependsOnId)
            .sorted()
            .toList();
    }

    /** 我的后继（谁依赖我） */
    @Transactional(readOnly = true)
    public List<String> dependentsOf(String taskId) {
        return depRepo.findByIdDependsOnId(taskId).stream()
            .map(TaskDependency::getTaskId)
            .sorted()
            .toList();
    }

    /**
     * 检查 taskId 的前置是否全部完成。
     *
     * @return 未完成的前置 id 列表（空表示可以完成）
     */
    @Transactional(readOnly = true)
    public List<String> unfinishedPrerequisites(String taskId) {
        List<String> prereqs = prerequisitesOf(taskId);
        if (prereqs.isEmpty()) {
            return List.of();
        }
        Map<String, Task> byId = new HashMap<>();
        taskRepo.findAllById(prereqs).forEach(t -> byId.put(t.getId(), t));

        List<String> unfinished = new ArrayList<>();
        for (String p : prereqs) {
            Task t = byId.get(p);
            // 前置被删除也视为"未完成"——不阻止，但会在依赖校验里体现
            if (t == null || t.isDeleted() || t.getStatus() != TaskStatus.DONE) {
                unfinished.add(p);
            }
        }
        return unfinished;
    }

    /** 所有依赖边，供拓扑排序 / 重排期求解器使用 */
    @Transactional(readOnly = true)
    public List<TaskDependency> allEdges() {
        return depRepo.findAllByOrderByIdTaskIdAscIdDependsOnIdAsc();
    }

    // -----------------------------------------------------------------
    // 环检测
    // -----------------------------------------------------------------

    /**
     * 从 start 出发，沿"依赖"方向能否到达 target。
     *
     * 边方向：task -depends_on-> dependsOnId。
     * 要判断"加 taskId -> dependsOnId 会不会成环"，
     * 就要看 dependsOnId 能否沿依赖链回到 taskId。
     */
    boolean reaches(String start, String target) {
        if (start.equals(target)) {
            return true;
        }
        Set<String> visited = new HashSet<>();
        Deque<String> stack = new ArrayDeque<>();
        stack.push(start);
        visited.add(start);

        while (!stack.isEmpty()) {
            String cur = stack.pop();
            for (TaskDependency d : depRepo.findByIdTaskId(cur)) {
                String next = d.getDependsOnId();
                if (next.equals(target)) {
                    return true;
                }
                if (visited.add(next)) {
                    stack.push(next);
                }
            }
        }
        return false;
    }

    /**
     * 全图环检测。评测与种子数据灌入后跑一次，作为数据完整性断言。
     *
     * @return 存在环则返回环上的节点，否则空列表
     */
    @Transactional(readOnly = true)
    public List<String> findAnyCycle() {
        List<TaskDependency> edges = allEdges();
        Map<String, List<String>> adj = new HashMap<>();
        for (TaskDependency d : edges) {
            adj.computeIfAbsent(d.getTaskId(), k -> new ArrayList<>()).add(d.getDependsOnId());
        }

        Set<String> white = new HashSet<>(adj.keySet());
        Set<String> gray = new HashSet<>();
        Set<String> black = new HashSet<>();

        for (String node : new ArrayList<>(white)) {
            if (black.contains(node)) continue;
            List<String> cycle = dfsFindCycle(node, adj, gray, black);
            if (!cycle.isEmpty()) {
                return cycle;
            }
        }
        return List.of();
    }

    private List<String> dfsFindCycle(String start,
                                      Map<String, List<String>> adj,
                                      Set<String> gray,
                                      Set<String> black) {
        record Frame(String node, int idx) {}
        Deque<Frame> stack = new ArrayDeque<>();
        Deque<String> path = new ArrayDeque<>();

        stack.push(new Frame(start, 0));
        gray.add(start);
        path.push(start);

        while (!stack.isEmpty()) {
            Frame f = stack.pop();
            List<String> nbrs = adj.getOrDefault(f.node(), List.of());

            if (f.idx() < nbrs.size()) {
                String next = nbrs.get(f.idx());
                // 推回当前帧，索引 +1
                stack.push(new Frame(f.node(), f.idx() + 1));

                if (gray.contains(next)) {
                    // 找到环：从 path 里截取
                    List<String> cycle = new ArrayList<>();
                    for (String p : path) {
                        cycle.add(p);
                        if (p.equals(next)) break;
                    }
                    cycle.add(next);
                    return cycle;
                }
                if (!black.contains(next)) {
                    gray.add(next);
                    path.push(next);
                    stack.push(new Frame(next, 0));
                }
            } else {
                gray.remove(f.node());
                black.add(f.node());
                if (!path.isEmpty() && path.peek().equals(f.node())) {
                    path.pop();
                }
            }
        }
        return List.of();
    }

    // -----------------------------------------------------------------
    private Task requireLive(String id) {
        Task t = taskRepo.findById(id)
            .orElseThrow(() -> new TaskNotFoundException(id));
        if (t.isDeleted()) {
            throw new TaskNotFoundException(id);
        }
        return t;
    }
}
