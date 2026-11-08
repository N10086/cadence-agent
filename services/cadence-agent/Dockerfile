# syntax=docker/dockerfile:1
# 构建上下文是仓库根（与 compose 一致）。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data/agent

WORKDIR /app
COPY services/cadence-agent/pyproject.toml /app/pyproject.toml
COPY services/cadence-agent/src /app/src
RUN pip install .

RUN mkdir -p /data/agent
EXPOSE 18000
CMD ["uvicorn", "cadence.main:app", "--host", "0.0.0.0", "--port", "18000", "--workers", "1"]
