# OrionaMesh

个人知识库 RAG MVP：前后端分离、**后端优先**的私有知识库问答应用。用户上传 PDF、DOCX、
Markdown、纯文本资料，系统异步解析、分块、嵌入后，在绑定知识库的对话中以流式回答并附带
可核验的来源引用；证据不足时明确拒答，绝不编造。MVP 面向个人与轻量团队，全部资源按用户
隔离，外部模型调用统一经过内部出口网关（先脱敏、后发送、fail-closed）。

## 文档导航

开发前请先阅读 `AGENTS.md` 与下列权威文档（发生冲突时以它们为准，README 不复制业务规则）：

| 文档 | 职责 |
|---|---|
| [`specs/001-orionamesh-rag-mvp/spec.md`](specs/001-orionamesh-rag-mvp/spec.md) | 用户需求、范围、验收场景与成功标准 |
| [`specs/001-orionamesh-rag-mvp/plan.md`](specs/001-orionamesh-rag-mvp/plan.md) | 架构、模块边界与实施顺序 |
| [`specs/001-orionamesh-rag-mvp/data-model.md`](specs/001-orionamesh-rag-mvp/data-model.md) | 数据不变量、状态机与事务边界 |
| [`specs/001-orionamesh-rag-mvp/contracts/openapi.yaml`](specs/001-orionamesh-rag-mvp/contracts/openapi.yaml) | 版本化 REST/SSE 契约与错误码 |
| [`specs/001-orionamesh-rag-mvp/contracts/model-egress.md`](specs/001-orionamesh-rag-mvp/contracts/model-egress.md) | 模型出口、脱敏与审计边界 |
| [`specs/001-orionamesh-rag-mvp/quickstart.md`](specs/001-orionamesh-rag-mvp/quickstart.md) | 环境变量、依赖安装、部署与验证步骤 |
| [`specs/001-orionamesh-rag-mvp/tasks.md`](specs/001-orionamesh-rag-mvp/tasks.md) | 实施任务顺序与交付物 |

## 技术栈

- **后端**：Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、Celery、Redis、
  PostgreSQL 16（pgvector + pg_trgm）、LangChain（OpenAI-compatible 协议）、structlog、PyJWT
- **前端**（Phase 7 后开始）：Next.js、React、TypeScript、Tailwind CSS、Shadcn/UI、Pino
- **依赖管理**：后端 `uv`（提交 `backend/uv.lock`）；前端 `pnpm`（根目录唯一 `pnpm-lock.yaml`）

## 仓库结构

```text
backend/        # FastAPI 后端（app/、migrations/、tests/）
specs/          # 功能规格、计划、数据模型、契约与任务（权威来源）
scripts/        # 质量门禁脚本（check-backend.sh 等）
frontend/       # Next.js 前端（尚未开始，T105 门禁通过后创建）
deploy/         # Docker Compose 编排（Phase 7 创建）
.github/        # CI/CD（Phase 7 创建）
```

## 当前状态

- ✅ **阶段 1（后端项目与通用基础设施，T001–T013）已完成**：项目骨架、uv 锁文件、质量工具链、
  FastAPI 入口与 `/health`、统一 `code/data/msg/trace_id` 信封、trace_id 中间件、
  structlog JSON 日志（敏感字段递归脱敏）、SQLAlchemy/Alembic 与初始扩展迁移
  （vector、pg_trgm、pgcrypto）。
- ⏳ **接下来**：阶段 2（数据模型、身份认证、知识库边界、分级限流与统一模型出口）。
- **严格后端优先**：阶段 1–7 只开发后端与工程化；前端 UI 在 T105 门禁通过前禁止开始。

## 本地开发（后端）

前置条件：64 位 Python 3.12 与 [uv](https://docs.astral.sh/uv/)。

```bash
cd backend
uv sync --locked          # 按 uv.lock 安装依赖（含 dev 组）
uv run orionamesh-api     # 启动 API（默认 0.0.0.0:8000）
# 或开发热重载：uv run uvicorn app.main:app --reload
```

常用命令：

```bash
uv run pytest                    # 运行全部测试
uv run ruff format . && uv run ruff check .   # 格式化与静态检查
uv run pyright                   # 类型检查
bash ../scripts/check-backend.sh # 一键质量门禁（lock/sync/ruff/pyright/pytest）
```

数据库迁移（需要 PostgreSQL）：

```bash
uv run alembic upgrade head            # 执行迁移
uv run alembic upgrade head --sql      # 离线生成 SQL（无数据库时预览）
```

环境变量见 `quickstart.md` 的配置契约；当前最小集只需 `DATABASE_URL`（含本地开发默认值），
阶段 2 起新增认证、限流与模型网关必填项。`/health` 可在启动后直接访问验证。

> **Windows 提示**：`psycopg-binary` 等二进制依赖只有 64 位 wheel，请确保 PATH 中的
> `uv`/Python 为 64 位安装（本项目以 `backend/.python-version` 固定 Python 3.12.12）。

## 验证

后端功能验证清单（认证、租户隔离、上传、流水线、删除、SSE、限流、模型出口安全等 25 项）
见 `quickstart.md` 的「后端优先验证」。阶段门禁：T091（A5 契约冻结）→ T105（A6 工程化与部署）
→ 之后才允许前端任务。

## License

[MIT](LICENSE)
