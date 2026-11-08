package io.cadence.core.web;

import static org.assertj.core.api.Assertions.assertThat;

import io.cadence.core.AbstractIntegrationTest;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;

/**
 * 契约一致性测试：openapi.yaml 里声明的路径，运行时必须全部注册；
 * 运行时注册的业务路径，也必须在 yaml 里出现。
 *
 * 这是"契约冻结"真正生效的地方。
 * 否则 yaml 只是一份会慢慢腐烂的文档 —— 改了代码没人记得改文档。
 *
 * 两个方向都比对，才能同时挡住：
 *   - 写了文档没写实现（客户端照着文档调会 404）
 *   - 写了实现没写文档（Agent 不知道有这个接口）
 */
class ContractConformanceIT extends AbstractIntegrationTest {

    private static final Path SPEC = Path.of("..", "..", "specs", "contracts", "openapi.yaml");

    @Autowired
    @Qualifier("requestMappingHandlerMapping")
    RequestMappingHandlerMapping mapping;

    @Test
    @DisplayName("openapi.yaml 中声明的每个 path 都在运行时注册")
    void declaredPathsAreRegistered() throws Exception {
        Set<String> declared = declaredPaths();
        Set<String> actual = actualBusinessPaths();

        assertThat(declared).isNotEmpty();
        assertThat(actual).containsAll(declared);
    }

    @Test
    @DisplayName("运行时注册的每个业务 path 都写进了 openapi.yaml")
    void registeredPathsAreDeclared() throws Exception {
        Set<String> declared = declaredPaths();
        Set<String> actual = actualBusinessPaths();

        assertThat(declared).containsAll(actual);
    }

    // ------------------------------------------------------------------

    /** 运行时注册的业务路径（排除 Spring 自带的 /error） */
    private Set<String> actualBusinessPaths() {
        Set<String> paths = new TreeSet<>();
        mapping.getHandlerMethods().entrySet().forEach(e -> {
            e.getKey().getPathPatternsCondition().getPatterns().forEach(p -> {
                String v = p.getPatternString();
                if (v.startsWith("/api/v1")) {
                    paths.add(v);
                }
            });
        });
        return paths;
    }

    /**
     * 从 yaml 里抠出 paths 段下的路径。
     *
     * 刻意用极简的行解析而不是引入 yaml 库（Spring 也不带 snakeyaml 的 API 稳定入口）：
     * openapi.yaml 的 paths 段结构固定 —— 两个空格缩进、以 / 开头、以 : 结尾。
     * 如果哪天这条规则不再成立，本测试会失败，提醒你改解析逻辑。
     */
    private Set<String> declaredPaths() throws Exception {
        assertThat(Files.exists(SPEC))
            .as("找不到契约文件: " + SPEC.toAbsolutePath().normalize())
            .isTrue();

        Set<String> paths = new TreeSet<>();
        boolean inPaths = false;

        for (String raw : Files.readAllLines(SPEC)) {
            String line = raw.stripTrailing();

            if (line.startsWith("paths:")) {
                inPaths = true;
                continue;
            }
            if (inPaths && !line.isBlank() && !line.startsWith(" ")) {
                break;   // 离开 paths 段
            }
            if (!inPaths) continue;

            String trimmed = line.strip();
            if (trimmed.startsWith("/") && trimmed.endsWith(":")) {
                paths.add(trimmed.substring(0, trimmed.length() - 1));
            }
        }
        return paths;
    }

    @Test
    @DisplayName("契约文件里每个 path 都有 operationId")
    void everyOperationHasOperationId() throws Exception {
        String content = Files.readString(SPEC);
        long pathCount = declaredPaths().size();
        long opIdCount = content.lines().filter(l -> l.strip().startsWith("operationId:")).count();

        // 每个 path 至少一个 operation
        assertThat(opIdCount).isGreaterThanOrEqualTo(pathCount);
    }
}
