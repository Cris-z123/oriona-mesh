# oriona-mesh Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-08-12

## Active Technologies
- PostgreSQL（业务与任务真相）、Redis（队列及临时限流计数）、存储后端无关的文件存储 (001-orionamesh-rag-mvp)
- Python 3.12（后端）；TypeScript 5.x（前端） + FastAPI、Pydantic、SQLAlchemy、Alembic、Celery、Redis、LangChain、structlog、PostgreSQL、pgvector、pg_trgm、JWT；Next.js、React、TypeScript、Tailwind CSS、Shadcn/UI、Pino (001-orionamesh-rag-mvp)
- Python 3.12；Node.js 22 LTS；TypeScript 5.x + FastAPI、Pydantic v2、LangChain、Celery、Redis、SQLAlchemy 2、Alembic、psycopg、PyJWT、structlog；Next.js、React、Tailwind CSS、shadcn/ui、pino (001-orionamesh-rag-mvp)
- PostgreSQL 16（`pgvector`、`pg_trgm`）；Redis 7 仅用于队列、缓存与瞬时限流计数；文件存储通过抽象接口接入本地卷或对象存储 (001-orionamesh-rag-mvp)
- Python 3.12、TypeScript 5、Node.js LTS + FastAPI、Pydantic、LangChain、Celery、SQLAlchemy/Alembic、Next.js、React、Tailwind CSS、Shadcn/UI、structlog、Pino (001-orionamesh-rag-mvp)
- PostgreSQL 16（`pgvector`、`pg_trgm`）；Redis 7 仅用于队列、缓存与瞬时限流计数；MVP 文件存储使用挂载到 `/data/orionamesh` 的本地持久卷，数据库仅保存相对对象键；对象存储通过同一抽象接口后续扩展 (001-orionamesh-rag-mvp)
- Python 3.12；TypeScript 5.x；Node.js 22 LTS + FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、Celery、Redis、LangChain、structlog、PyJWT；Next.js、React、Tailwind CSS、Shadcn/UI、Pino (001-orionamesh-rag-mvp)
- PostgreSQL 16（pgvector、pg_trgm）；Redis 7 仅用于队列与瞬时限流；MVP 使用挂载到 `/data/orionamesh` 的本地持久卷并只保存相对对象键 (001-orionamesh-rag-mvp)



## Project Structure

```text
src/
tests/
```

## Commands

# Add commands for 

## Code Style

General: Follow standard conventions

## Recent Changes
- 001-orionamesh-rag-mvp: Added Python 3.12；TypeScript 5.x；Node.js 22 LTS + FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、Celery、Redis、LangChain、structlog、PyJWT；Next.js、React、Tailwind CSS、Shadcn/UI、Pino
- 001-orionamesh-rag-mvp: Added Python 3.12；TypeScript 5.x；Node.js 22 LTS + FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、Celery、Redis、LangChain、structlog、PyJWT；Next.js、React、Tailwind CSS、Shadcn/UI、Pino
- 001-orionamesh-rag-mvp: Added Python 3.12、TypeScript 5、Node.js LTS + FastAPI、Pydantic、LangChain、Celery、SQLAlchemy/Alembic、Next.js、React、Tailwind CSS、Shadcn/UI、structlog、Pino



<!-- MANUAL ADDITIONS START -->
## OrionaMesh MVP（001-orionamesh-rag-mvp）

- 后端：Python 3.12、FastAPI、Pydantic、SQLAlchemy、Alembic、Celery、Redis、
  PostgreSQL（pgvector、pg_trgm）、LangChain 与 structlog；依赖使用 uv 和 `uv.lock`。
- 前端：TypeScript、Next.js、React、Tailwind CSS、Shadcn/UI 与 Pino；依赖使用 pnpm 和
  `pnpm-lock.yaml`。
- 代码结构为 `backend/app/{api/v1,core,db,models,repositories,services,workers}` 和
  `frontend/{app,components,features,lib/api}`；测试位于 `backend/tests/` 与 `frontend/tests/`。
- 实施顺序不可调整：先完成并验证后端业务逻辑与 `/v1` REST/SSE 契约，再开始前端渲染。
- 所有资源必须服务端按当前用户授权；检索强制过滤用户、知识库、完成状态与当前资料版本。
- `embed` 幂等直写 `chunks`，`finalize` 只校验并翻转资料状态；所有片段读取必须经统一
  `ChunkRepository`，禁止路由、服务或 worker 直接读取该表。
- 质量门禁：Ruff、Pyright、pytest、OpenAPI 校验；ESLint、Prettier、TypeScript、Vitest 和
  Playwright；Docker Compose 与 GitHub Actions 必须使用锁定依赖安装。
<!-- MANUAL ADDITIONS END -->
