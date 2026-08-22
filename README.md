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
- **前端**：Next.js · React · TypeScript · Tailwind CSS · shadcn/ui · TanStack Query · Zustand · next-themes · Vitest
- **工具链**：uv · pnpm（根工作区唯一锁文件）· Ruff · Pyright · pytest · ESLint · Prettier · Docker Compose · GitHub Actions

## 快速开始

### 前置条件

- **Python 3.12（64 位）** 与 [uv](https://docs.astral.sh/uv/)；Windows 上请确保 PATH 中的
  Python/uv 为 64 位安装（二进制依赖如 `psycopg` 没有 32 位 wheel）。
- **PostgreSQL 16**，并已启用扩展：`pgvector`、`pg_trgm`（UUID 支持由 `pgcrypto` 提供，
  迁移会自动创建）。
- **Redis 7**：异步任务队列与瞬时限流共享；数据库是任务状态唯一真相源，Redis 丢失可恢复。
- **模型网关 endpoint 与凭证**：所有外部模型调用必须经过后端内部出口网关；本地开发可使用
  `localhost`/`127.0.0.1` 回环 HTTP endpoint，非回环 HTTP endpoint 会被拒绝就绪（见「配置」）。

### 启动后端

```bash
git clone https://github.com/Cris-z123/oriona-mesh.git
cd oriona-mesh/backend

uv sync --locked            # 按 uv.lock 安装依赖（含开发依赖）
uv run alembic upgrade head # 执行数据库迁移（需要 PostgreSQL 在线）
uv run orionamesh-api       # 启动 API，默认监听 0.0.0.0:8000
```

资料异步处理、维护扫描器与删除清理由 Celery worker 执行；本地开发需另开终端启动（`-B` 启用
周期扫描器）：

```bash
uv run celery -A app.workers.celery_app worker -B --loglevel=info
```

启动后验证：

```bash
curl http://localhost:8000/health
# => {"code":0,"data":{"status":"ok"},"msg":"","trace_id":"<uuid>"}
```

开发热重载：`uv run uvicorn app.main:app --reload`。

### 启动前端

```bash
cd oriona-mesh    # 仓库根目录（pnpm 工作区）
pnpm install --frozen-lockfile   # 按根目录唯一 pnpm-lock.yaml 安装（根工作区策略，禁止新增其他锁文件）
pnpm dev                         # Next.js 开发服务器，默认 http://localhost:3000
```

浏览器端 API 基地址固定为同源 `/v1`：本地开发由 Next.js 开发服务器把 `/v1/*` 代理到
`ORIONAMESH_API_DEV_UPSTREAM`（默认 `http://127.0.0.1:8000`），因此无需为开发便利放宽后端
CORS；若本地 API 使用其他地址，只设置该非公开变量并重启 `pnpm dev`。生产环境由 Nginx 同源
转发 `/v1/*`，不启用此开发代理。

前端覆盖登录/注册、本人基本资料、知识库管理、受限批量上传与资料状态轮询、绑定知识库的会话、
SSE 流式问答与来源引用抽屉；所有错误统一按 `code/msg/trace_id` 呈现，不显示重处理/替换入口。

### 配置

后端配置通过环境变量注入，唯一的配置入口是 `backend/app/core/settings.py`。环境模板
（已提交、可复制的示例）：`backend/.env.local.example`（本地开发）、`backend/.env.test.example`
（自动化测试）、`.env.example`（部署参考）与 `frontend/.env.example`（前端）。所有实际
`.env.local` / `.env.test` 文件均被 gitignore 排除，部署环境（staging/production）不读取
仓库内任何 `.env` 文件，由 Docker/CI 注入。完整的环境变量契约见
[quickstart.md](specs/001-orionamesh-rag-mvp/quickstart.md)。关键行为边界：

- **本地持久卷**：原始文件与解析对象保存在 `DOCUMENT_STORAGE_ROOT`（默认 `/data/orionamesh`），
  数据库只保存相对对象键；容器重建后资料必须保留，路径逃逸被拒绝。
- **受限上传与幂等**：单文件 ≤50MB、单批 ≤20 个，任一项不满足整批拒绝且零副作用；批量上传
  使用请求级 `Idempotency-Key`（结果保留 24 小时），同键重放返回首次结果、协调中返回
  `20008/409`。
- **解析安全**：PDF/DOCX/Markdown/TXT 分别由 PyMuPDF、python-docx、markdown-it-py、
  charset-normalizer 在子进程中解析并受超时保护；拒绝宏/脚本/外链、路径穿越、压缩炸弹与
  解压大小超限。
- **处理并发**：单用户同时最多 `DOCUMENT_PROCESSING_MAX_PER_USER`（默认 3）份资料进入
  processing，名额跨 parse → chunk → embed → finalize 持续持有，worker 失联后由恢复扫描器
  按租约回收。
- **可信代理限流**：`RATE_LIMIT_TRUSTED_PROXY_CIDRS` 为空时忽略全部转发头；命中可信 CIDR
  时从右向左取首个非可信来源 IP，非法链回退直连对端；限流键均为不可逆摘要，原始邮箱、令牌
  与完整转发链不进入 Redis 或日志。
- **模型出口网关**：Embedding、查询改写、Reranker 与回答生成全部经内部网关，先脱敏、后发送、
  失败即拒绝（fail-closed），日志只保留元数据白名单；endpoint 必须是 HTTPS（本机回环 HTTP
  例外），未知 provider 或缺失必填模型时拒绝就绪。
- **必填安全配置**：`AUTH_JWT_SECRET_KEY`（UTF-8 ≥32 字节，仅用于 HS256 Access Token 签名）、
  `RATE_LIMIT_SUBJECT_HMAC_KEY`、`MODEL_GATEWAY_ENDPOINT`/`MODEL_GATEWAY_API_KEY` 与必填模型
  缺失时应用启动直接失败；`MODEL_GATEWAY_AUDIT_PAYLOADS` 必须保持 `false`，启动校验拒绝
  开启正文日志。

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
frontend/              # Next.js 前端（认证、知识库、资料、会话与 SSE 问答）
  src/app/             # App Router 页面（auth / knowledge-bases / conversations / profile）
  src/features/        # 业务渲染（认证、资料、文档任务历史、消息流、引用抽屉等）
  src/lib/             # API 客户端、TanStack Query 封装、Pino 服务端日志（脱敏）
  src/stores/          # Zustand UI 状态（仅导航折叠/抽屉/视图偏好，不存服务端实体）
  tests/               # unit / component
specs/                 # 功能规格与权威契约文档
scripts/               # 质量门禁脚本（check-backend.sh / verify-contracts.sh / check-frontend.sh）
deploy/                # Docker 镜像（docker/）与 Compose 编排（compose/）
.github/workflows/     # CI（ci.yml）与镜像构建/扫描、正式 tag GitHub Release 交付（image.yml）
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
bash scripts/check-frontend.sh   # 一键前端门禁（frozen-lockfile → lint → format → tsc → vitest）

bash scripts/verify-contracts.sh # 契约与部署基线（OpenAPI/迁移离线 SQL/配置契约，可独立运行）
```

数据库迁移可用离线模式预览而不需要数据库：`uv run alembic upgrade head --sql`。

## 部署

### Docker Compose（单机）

`deploy/compose/compose.yaml` 编排 PostgreSQL（pgvector）、Redis、one-off 迁移、后端
API、Celery worker、前端与 Nginx。仅 Nginx 发布 80；API/worker 共用 `BACKEND_IMAGE` 与
`/data/orionamesh` 命名持久卷，前端使用 `FRONTEND_IMAGE`。应用镜像必须来自已校验的
GitHub Release，Compose 不包含 `build` 配置。

腾讯云单机的首次部署、GitHub Release 下载与 SHA-256 校验、导入、升级、回滚和安全组规则，
以 [quickstart.md](specs/001-orionamesh-rag-mvp/quickstart.md#github-release-单机部署腾讯云-ubuntu-x86_64)
为唯一运行手册：发布包内的 `scripts/deploy.sh` 按「校验归档 → `docker image load` 导入应用镜像
→ PostgreSQL/Redis 健康 → 串行 one-off `alembic upgrade head` → `docker compose up --no-build
--pull never`」的顺序启动应用服务；服务器只导入 GitHub Release 归档中的应用镜像，不构建应用镜像、
不访问 GHCR，也不用 `latest` 作为运行引用。

### 镜像门禁（GitHub Actions）

- `image.yml` 在 PR 与正式 Git tag（`v*`）构建 `linux/amd64` backend/frontend 双镜像并执行
  Trivy 漏洞扫描（HIGH/CRITICAL 即失败）；正式 tag 额外生成带 SHA-256 文件的 GitHub Release
  归档，服务器仅导入归档中的应用镜像。main 为受保护分支（仅 PR 合并），合并后不重复构建镜像。

### 升级与回滚

- 升级：下载并校验新的 GitHub Release，导入镜像，执行 one-off migrate，成功后以
  `--no-build --pull never` 更新应用并等待健康检查。
- 回滚：导入上一已验证 Release 的镜像并重复部署；镜像回滚**不会自动降级数据库**。破坏性迁移发布前
  必须先人工备份，失败时停止发布并按备份恢复。

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
