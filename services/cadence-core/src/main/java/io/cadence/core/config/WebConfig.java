package io.cadence.core.config;

import java.nio.file.Path;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.ResourceHandlerRegistry;
import org.springframework.web.servlet.config.annotation.ViewControllerRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * 前端页面托管 + 本地开发跨域。
 *
 * 两件事：
 *
 * 1. 把 cadence-web 的静态文件挂到 /ui/**，访问 http://localhost:18080/ 自动跳到 /ui/index.html。
 *    这样"起一个后端就有页面"，不用另开静态服务器，也不会出现跨域。
 *
 * 2. 允许前端独立打开（file:// 或别的端口的静态服务器）时跨域调用 /api/**。
 *    只对 /api/** 放开，且是单用户本地系统，不存在越权问题（无登录体系，见 PRD Non-Goal）。
 *
 * 静态目录解析顺序：
 *   - 若设置了环境变量 CORE_WEB_DIR，用该目录（部署/自定义位置）
 *   - 否则用 ../cadence-web/src/main/resources/static（仓库内直接 mvn spring-boot:run 的场景）
 *   - 再否则用 ../cadence-web/build（打包后的位置）
 */
@Configuration
public class WebConfig implements WebMvcConfigurer {

    private static final String[] WEB_CANDIDATES = {
        "../cadence-web/src/main/resources/static",
        "../cadence-web/build",
        "cadence-web"
    };

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/api/**")
            .allowedOriginPatterns("*")
            .allowedMethods("GET", "POST", "PATCH", "DELETE", "OPTIONS")
            .allowedHeaders("*")
            .exposedHeaders("Idempotent-Replayed", "Location")
            .maxAge(3600);
    }

    @Override
    public void addResourceHandlers(ResourceHandlerRegistry registry) {
        Path dir = resolveWebDir();
        System.out.println("[WEB] static root = " + dir + " (exists=" + dir.toFile().isDirectory() + ")"
            + " user.dir=" + System.getProperty("user.dir"));
        // /ui/** 与根路径都指向前端目录；工作目录是 services/cadence-core 或仓库根都能工作
        registry.addResourceHandler("/ui/**")
            .addResourceLocations(dir.toUri().toString());
        registry.addResourceHandler("/**")
            .addResourceLocations(dir.toUri().toString());
    }

    @Override
    public void addViewControllers(ViewControllerRegistry registry) {
        registry.addRedirectViewController("/", "/ui/index.html");
        registry.addViewController("/ui/").setViewName("forward:/ui/index.html");
    }

    /** 解析前端静态目录（公开给测试断言用） */
    public static Path resolveWebDir() {
        String override = System.getenv("CORE_WEB_DIR");
        if (override != null && !override.isBlank()) {
            return Path.of(override);
        }
        for (String candidate : WEB_CANDIDATES) {
            Path p = Path.of(candidate).toAbsolutePath().normalize();
            if (p.toFile().isDirectory()) {
                return p;
            }
        }
        // 都不存在时返回第一个，让 404 的路径信息更有指向性
        return Path.of(WEB_CANDIDATES[0]).toAbsolutePath().normalize();
    }
}
