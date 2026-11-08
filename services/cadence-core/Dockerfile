# syntax=docker/dockerfile:1
# 构建上下文是仓库根：Core 需要同时拷入 services/cadence-web 的静态文件。
FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /workspace

COPY services/cadence-core/pom.xml services/cadence-core/pom.xml
RUN mvn -B -f services/cadence-core/pom.xml -DskipTests dependency:go-offline

COPY services/cadence-core/src services/cadence-core/src
RUN mvn -B -f services/cadence-core/pom.xml -DskipTests package

FROM eclipse-temurin:21-jre
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /workspace/services/cadence-core/target/cadence-core-*.jar /app/app.jar
# WebConfig 用 CORE_WEB_DIR 找前端；容器里不能靠 ../cadence-web 相对路径。
COPY services/cadence-web/src/main/resources/static /app/web
ENV CORE_WEB_DIR=/app/web \
    CORE_PORT=18080
EXPOSE 18080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
