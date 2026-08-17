# syntax=docker/dockerfile:1
# OrionaMesh 前端镜像（Next.js standalone 输出，T098）。
# 构建上下文必须是仓库根目录（依赖根 pnpm 工作区与唯一 pnpm-lock.yaml）：
#   docker build -f deploy/docker/frontend.Dockerfile -t orionamesh-frontend .
#
# 约束（quickstart 部署契约）：
# - 使用根目录唯一 pnpm-lock.yaml（pnpm install --frozen-lockfile），
#   锁文件缺失或过期时构建直接失败；
# - 前端只消费后端 /v1 契约，NEXT_PUBLIC_API_BASE_URL 由部署环境注入。

# ---------- 基础阶段：固定 pnpm 版本（与本地工具链、CI 一致） ----------
FROM node:22-slim AS base
# npm 注册源可在构建时覆盖（国内服务器可用 npmmirror）；默认官方源。
ARG NPM_REGISTRY=https://registry.npmjs.org/
RUN npm install -g pnpm@10.28.2 --registry=${NPM_REGISTRY}
ENV NEXT_TELEMETRY_DISABLED=1

# ---------- 依赖阶段：按根锁文件安装全部依赖（含 devDependencies，构建需要） ----------
FROM base AS deps
ARG NPM_REGISTRY=https://registry.npmjs.org/
WORKDIR /repo
# .npmrc 必须随构建进入镜像：node-linker=hoisted 是 Next standalone 追踪的前提。
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml .npmrc ./
COPY frontend/package.json ./frontend/
RUN pnpm install --frozen-lockfile --registry=${NPM_REGISTRY}

# ---------- 构建阶段：复制源码与 node_modules 后执行 next build ----------
FROM base AS builder
WORKDIR /repo
# node-linker=hoisted：全部依赖平铺在 /repo/node_modules（frontend/ 下无 node_modules）。
COPY --from=deps /repo/node_modules ./node_modules
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml .npmrc ./
COPY frontend ./frontend
RUN pnpm --filter frontend build

# ---------- 运行时阶段：仅复制 standalone 最小输出 ----------
FROM node:22-slim AS runtime
LABEL org.opencontainers.image.title="orionamesh-frontend" \
      org.opencontainers.image.description="OrionaMesh RAG MVP frontend (Next.js)" \
      org.opencontainers.image.source="https://github.com/Cris-z123/oriona-mesh"

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    # Docker 会注入 HOSTNAME（容器 ID），Next standalone 会绑定到该主机名而非 0.0.0.0，
    # 导致容器内 127.0.0.1 健康检查失败；固定为 0.0.0.0。
    HOSTNAME=0.0.0.0

WORKDIR /app
# pnpm 工作区下 standalone 输出位于 .next/standalone/frontend/（含 server.js 与最小 node_modules）。
COPY --from=builder /repo/frontend/.next/standalone ./
COPY --from=builder /repo/frontend/.next/static ./frontend/.next/static
# Turbopack 的 standalone 输出不完整包含 .next/server（SSR chunk 部分缺失，
# 运行时抛 ChunkLoadError）；合并完整 server 目录补全。
COPY --from=builder /repo/frontend/.next/server ./frontend/.next/server

USER node
EXPOSE 3000
CMD ["node", "frontend/server.js"]
