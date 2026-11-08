package io.cadence.core.web;

import io.cadence.core.service.BatchService;
import io.cadence.core.web.dto.BatchUpdateRequest;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 批量更新控制器。
 *
 * 单独成类的原因：路径必须是精确的 POST /api/v1/tasks:batch。
 *
 * Spring MVC 的路径规范化会把方法级 ":batch" 拼成 /{类级前缀}/:batch，
 * 多出一个斜杠。而 "{id}:complete" 能工作，是因为 {id} 是一个合法路径段。
 * 所以 ":batch" 这种「路径段内部的冒号」必须出现在一个不含变量、且不能由
 * 类级前缀推导出来的位置 —— 把类级前缀设成 /api/v1，方法路径写 /tasks:batch，
 * 拼出来正好是 /api/v1/tasks:batch。
 *
 * 这个约束是 Spring MVC 的实现细节，写在这里是为了不让下一个人再踩一遍。
 */
@RestController
@RequestMapping("/api/v1")
public class BatchController {

    private final BatchService batchService;

    public BatchController(BatchService batchService) {
        this.batchService = batchService;
    }

    @PostMapping("/tasks:batch")
    public BatchService.BatchResult batch(@Valid @RequestBody BatchUpdateRequest req,
                                          @RequestHeader(value = "X-Actor", required = false) String actor) {
        return batchService.apply(req);
    }
}
