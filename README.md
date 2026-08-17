# OrionaMesh

面向个人与轻量团队的**私有知识库 RAG 问答应用**：把你上传的 PDF、DOCX、Markdown 和纯文本
资料，转化成一个只属于你自己的知识库，并围绕它进行带来源引用的连续问答。

- **私有隔离**：每个用户的知识库、资料与对话相互隔离，所有访问在服务端按当前用户授权；
  检索与回答只使用已完成处理的当前版本资料。
- **可信回答**：回答以流式（SSE）返回并附带可定位的来源引用；知识库中没有相关证据时明确
  拒答，绝不把推测伪装成资料结论；证据不足时由两个独立召回通道（向量 + 关键词）的融合结果判定。
- **安全出口**：所有外部 Embedding、改写、重排与生成调用统一经过内部模型出口网关——
  先脱敏、后发送、失败即拒绝（fail-closed）；日志只保留调用元数据白名单，不记录请求/响应正文与凭证。
- **可诊断异步流水线**：资料经 `parse → chunk → embed → finalize` 异步处理，每个阶段、
  每次尝试与重试均有持久化记录；处理失败显示明确原因，绝无"永久处理中"。

## 技术栈

- **后端**：Python 3.12 · FastAPI · SQLAlchemy 2 · Alembic · Celery · Redis · PostgreSQL 16（pgvector / pg_trgm）
- **模型接入**：LangChain（OpenAI-compatible 协议）+ 统一出口网关（脱敏、路由、审计）
- **前端**：Next.js · React · TypeScript · Tailwind CSS · shadcn/ui · Vitest（骨架已完成，UI 渲染待后端 A6 门禁后的阶段 8 起）
- **工具链**：uv · pnpm（根工作区唯一锁文件）· Ruff · Pyright · pytest · ESLint · Prettier · Docker Compose · GitHub Actions

## 快速开始

### 前置条件

- **Python 3.12（64 位）** 与 [uv](https://docs.astral.sh/uv/)；Windows 上请确保 PATH 中的
  Python/uv 为 64 位安装（二进制依赖如 `psycopg` 没有 32 位 wheel）。
- **PostgreSQL 16**，并已启用扩展：`pgvector`、`pg_trgm`（UUID 支持由 `pgcrypto` 提供，
  迁移会自动创建）。

### 启动后端

```bash
git clone https://github.com/Cris-z123/oriona-mesh.git
cd oriona-mesh/backend

uv sync --locked            # 按 uv.lock 安装依赖（含开发依赖）
uv run alembic upgrade head # 执行数据库迁移（需要 PostgreSQL 在线）
uv run orionamesh-api       # 启动 API，默认监听 0.0.0.0:8000
```

启动后验证：

```bash
curl http://localhost:8000/health
# => {"code":0,"data":{"status":"ok"},"msg":"","trace_id":"<uuid>"}
```

开发热重载：`uv run uvicorn app.main:app --reload`。

### 启动前端（骨架）

```bash
cd orionames-mesh  # 仓库根目录（pnpm 工作区）
pnpm install --frozen-lockfile   # 按根目录唯一 pnpm-lock.yaml 安装
pnpm dev                         # Next.js 开发服务器，默认 http://localhost:3000
```

### 配置

后端配置通过环境变量注入，唯一的配置入口是 `backend/app/core/settings.py`。最小启动只需要
`DATABASE_URL`（未设置时使用本地开发默认值）；认证、限流与模型网关的必填变量在对应功能
交付后生效。环境模板（已提交、可复制的示例）：
`backend/.env.local.example`（本地开发）、`backend/.env.test.example`（自动化测试）、
`.env.example`（部署参考）与 `frontend/.env.example`（前端）。所有实际 `.env.local` /
`.env.test` 文件均被 gitignore 排除，部署环境（staging/production）不读取仓库内任何
`.env` 文件，由 Docker/CI 注入。完整的环境变量契约见
[quickstart.md](specs/001-orionamesh-rag-mvp/quickstart.md)。

## API

- 所有非 SSE 响应使用统一 JSON 信封：`{ "code": 0, "data": …, "msg": "", "trace_id": "<uuid>" }`；
  业务错误使用稳定错误码（如 `10001` 登录失效、`20002` 知识库不存在、`50000` 内部错误）。
- 每个请求可携带 `X-Trace-Id` 请求头透传链路标识，响应头会回写同一值。
- 业务接口规划在 `/v1` 下：`/auth/sessions`、`/users/me`、`/knowledge-bases`、
  `/knowledge-bases/{id}/documents`、`/conversations` 与消息 SSE 流；
  完整契约（请求/响应模式、错误码、限流策略、SSE 事件判别）以
  [openapi.yaml](specs/001-orionamesh-rag-mvp/contracts/openapi.yaml) 为唯一权威来源。

## 项目结构

```text
backend/               # FastAPI 后端
  app/
    api/v1/            # 版本化路由、传输 DTO、中间件与 SSE
    core/              # 唯一配置模块、日志（敏感字段脱敏）、安全基元
    db/                # ORM 声明基类
    infrastructure/    # 数据库会话、模型出口网关、限流、本地持久卷
    models/            # ORM 模型（领域实体）
    repositories/      # 统一仓储（租户范围、ChunkRepository、fencing）
    services/          # 业务编排（认证、上传、流水线、问答、删除）
    workers/           # Celery 任务与恢复/维护扫描器
  migrations/          # Alembic 迁移（0001 起：vector/pg_trgm/pgcrypto 扩展）
  tests/               # unit / integration / contract / architecture / security
frontend/              # Next.js 前端（骨架；业务渲染自阶段 8 起）
  src/app/             # App Router 根布局（阶段 7 无业务渲染）
  src/lib/             # 工具（cn）、Pino 服务端日志（脱敏）
  tests/               # unit / component / e2e（阶段 8 起）
specs/                 # 功能规格与权威契约文档
scripts/               # 质量门禁脚本（check-backend.sh / verify-contracts.sh / check-frontend.sh）
deploy/                # Docker 镜像（docker/）与 Compose 编排（compose/）
.github/workflows/     # CI（ci.yml）与 GHCR 镜像发布（image.yml）
```

## 测试与质量

```bash
cd backend
uv run pytest                    # 单元 / 集成 / 契约 / 架构测试
uv run ruff format .             # 格式化
uv run ruff check .              # 静态检查
uv run pyright                   # 类型检查
bash ../scripts/check-backend.sh # 一键后端门禁（lock → sync → ruff → pyright → pytest → 契约）

# 前端（仓库根目录，pnpm 工作区）
pnpm lint                        # ESLint
pnpm format:check                # Prettier
pnpm typecheck                   # TypeScript 严格检查
pnpm test                        # Vitest
bash scripts/check-frontend.sh   # 一键前端门禁（frozen-lockfile → lint → format → tsc → vitest → e2e）

bash scripts/verify-contracts.sh # 契约与部署基线（OpenAPI/迁移离线 SQL/配置契约，可独立运行）
```

数据库迁移可用离线模式预览而不需要数据库：`uv run alembic upgrade head --sql`。

## 部署

### Docker Compose（单机）

`deploy/compose/compose.yaml` 编排 PostgreSQL（pgvector）、Redis、one-off 迁移、后端
API、Celery worker 与前端；API/worker 共用 `BACKEND_IMAGE` 并共同挂载
`/data/orionamesh` 命名持久卷，前端使用 `FRONTEND_IMAGE`（本地省略镜像变量时回退到
Dockerfile 构建）。必填变量通过 `deploy/compose/.env` 提供（缺失时
`docker compose config` 直接报错）。

```bash
docker compose -f deploy/compose/compose.yaml up -d --build
```

**国内服务器部署（无 GHCR 依赖）**：`deploy/compose/.env.example` 提供全部必填变量模板；
`scripts/deploy.sh` 一键构建并启动（基础镜像走 Docker Hub 加速，npmjs/PyPI 可经
`NPM_REGISTRY` / `UV_INDEX_URL` 构建参数指向镜像源）；`deploy/nginx/nginx.conf` 提供
IP 同源反代示例（前端与 `/v1` API 同源，规避 CORS；SSE 已关闭缓冲）。有域名后追加
443 监听与证书即可。

### 镜像命名与发布（GitHub Actions）

- 镜像：`ghcr.io/${GITHUB_REPOSITORY}-backend` 与 `ghcr.io/${GITHUB_REPOSITORY}-frontend`
  （仓库路径转小写；`packages: write` 权限）。
- 标签：受保护分支 main 只打不可变 `sha-${GITHUB_SHA}`；正式 Git tag（`v*`）追加语义版本；
  **永不发布 `latest`**；构建后执行 Trivy 漏洞扫描（HIGH/CRITICAL 即失败）。

### 升级与回滚

- 升级顺序：`docker compose pull` → 串行 one-off migrate 容器执行
  `alembic upgrade head` → **成功后**才切换 API/worker/前端并执行健康检查。
  API/worker 启动命令不自动迁移；迁移失败时旧容器保持运行。
- 回滚：把 `BACKEND_IMAGE`/`FRONTEND_IMAGE` **成对**切换到上一已验证
  `sha-` 标签，执行 `docker compose pull && docker compose up -d`，再执行健康检查。
  镜像回滚**不会自动降级数据库**；破坏性迁移发布前必须先人工备份，失败时停止发布并按备份恢复。

## 文档导航

| 文档 | 内容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 开发指南：实施范围、文档权威边界、实施约束 |
| [`spec.md`](specs/001-orionamesh-rag-mvp/spec.md) | 需求、用户故事与验收标准 |
| [`plan.md`](specs/001-orionamesh-rag-mvp/plan.md) | 架构与实施计划 |
| [`data-model.md`](specs/001-orionamesh-rag-mvp/data-model.md) | 数据不变量、状态机与事务 |
| [`openapi.yaml`](specs/001-orionamesh-rag-mvp/contracts/openapi.yaml) | REST/SSE 接口契约与错误码 |
| [`model-egress.md`](specs/001-orionamesh-rag-mvp/contracts/model-egress.md) | 模型出口、脱敏与审计边界 |
| [`quickstart.md`](specs/001-orionamesh-rag-mvp/quickstart.md) | 环境变量、验证步骤与部署 |
| [`tasks.md`](specs/001-orionamesh-rag-mvp/tasks.md) | 实施任务清单 |

## License

[MIT](LICENSE)
