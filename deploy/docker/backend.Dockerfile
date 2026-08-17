# syntax=docker/dockerfile:1
# OrionaMesh 后端镜像（API 与 Celery worker 共用，T097）。
# 构建上下文必须是仓库根目录：
#   docker build -f deploy/docker/backend.Dockerfile -t orionamesh-backend .
#
# 约束（quickstart 部署契约）：
# - 依赖按 backend/uv.lock 锁定安装（uv sync --locked），不解析最新版本；
# - 镜像内不包含任何 .env 文件；staging/production 由 Docker/CI 注入环境变量；
# - 镜像默认只启动 uvicorn，不执行迁移（迁移由 compose 的 one-off migrate 服务负责，
#   见 deploy/compose/compose.yaml / T099）。

# ---------- 构建阶段：锁定安装依赖到独立 .venv ----------
# uv 官方镜像（ghcr.io/astral-sh/uv:0.12.3）只含 /uv 二进制；按官方模式复制二进制，
# 固定 0.12.3（与本地工具链、CI 的 astral-sh/setup-uv 一致，可复现构建）。
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never
# 包索引可在构建时覆盖（国内服务器可用 TUNA 等镜像）；默认官方 PyPI。
ARG UV_INDEX_URL=https://pypi.org/simple
ENV UV_INDEX_URL=${UV_INDEX_URL}

WORKDIR /app
# 先复制清单与锁文件，随后复制源码，最大化层缓存命中。
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./
# --locked：锁文件缺失或过期时构建直接失败（可复现构建契约）。
# --no-dev：运行时不需要 pytest/pyright 等开发依赖。
RUN uv sync --locked --no-dev

# ---------- 运行时阶段 ----------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="orionamesh-backend" \
      org.opencontainers.image.description="OrionaMesh RAG MVP backend API/worker" \
      org.opencontainers.image.source="https://github.com/Cris-z123/oriona-mesh"

# 非 root 运行；预建持久卷根目录并由 appuser 拥有，保证首次挂载命名卷继承所有权。
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/orionamesh \
    && chown -R appuser:appuser /data/orionamesh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=staging

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app ./app
COPY --from=builder /app/migrations ./migrations
COPY --from=builder /app/alembic.ini ./
# appuser 需要在工作目录可写（如 Celery Beat 的 celerybeat-schedule 落盘）。
RUN chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

# 默认命令为 API；Celery worker 在 compose 中覆盖为 celery 命令。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
