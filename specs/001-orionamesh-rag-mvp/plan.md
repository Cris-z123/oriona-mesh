# Implementation Plan: OrionaMesh 个人知识库 RAG MVP

**Branch**: `001-orionamesh-rag-mvp` | **Date**: 2026-08-12 | **Spec**: [spec.md](./spec.md)  
**Input**: `specs/001-orionamesh-rag-mvp/spec.md` 与 `docs/OrionaMesh.md`

## Summary

构建前后端分离、后端优先的个人知识库 RAG MVP。后端使用 FastAPI、PostgreSQL、pgvector、
Redis 和 Celery 实现认证、租户隔离、资料异步处理、双路召回、可信回答、引用快照与 SSE；
前端在后端 REST/SSE 契约冻结后使用 Next.js 实现渲染。数据库是任务状态唯一真相源，所有外部
模型调用经过统一出口网关。资料及知识库删除采用有界等待、租约恢复和数据库 fencing，防止
卡死 worker 在删除后继续写入。SC-001～SC-007 仅作为产品成功指标，不属于自动化测试、阶段
门禁或发布阻断条件；其对应功能行为仍按 FR 和用户故事进行确定性测试。

## Technical Context

**Language/Version**: Python 3.12；TypeScript 5.x；Node.js 22 LTS  
**Primary Dependencies**: FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、Celery、Redis、LangChain、structlog、PyJWT；Next.js、React、Tailwind CSS、Shadcn/UI、Pino  
**Storage**: PostgreSQL 16（pgvector、pg_trgm）；Redis 7 仅用于队列与瞬时限流；MVP 使用挂载到 `/data/orionamesh` 的本地持久卷并只保存相对对象键  
**Testing**: pytest、Ruff、Pyright、OpenAPI 校验、架构/契约/集成/安全测试；Vitest、Testing Library、Playwright、ESLint、Prettier、TypeScript  
**Target Platform**: Linux 容器；Docker Compose 单机部署；GitHub Actions CI/CD  
**Project Type**: 前后端分离 Web 应用，后端 REST API + SSE  
**Performance Goals**: SC-001～SC-007 的量化值为产品成功观察指标；当前实施阶段不建立固定评测集、比例断言、性能阈值门禁或发布阻断条件  
**Constraints**: Backend-First；JWT Access Token 2 小时、Refresh Token 7 天；单文件 50MB、单批 20 个；单用户最多 3 份资料 processing；外部模型出口 fail-closed；日志禁止 payload  
**Scale/Scope**: 个人与轻量团队开源 MVP；PDF、DOCX、Markdown、TXT；MVP 不支持 SSO、密码重置、资料重处理/替换或纯聊天

## Constitution Check

*GATE：Phase 0 前及 Phase 1 后均通过。*

- [x] 所有资源由服务端根据当前认证用户执行归属授权，客户端不能指定可信租户边界。
- [x] 派生表冗余保存 `user_id`；`chunks` 只能由统一 `ChunkRepository` 读取，检索强制过滤租户、知识库、完成状态和当前版本。
- [x] PostgreSQL 是任务状态真相源；Celery/Redis 仅执行或传输；任务、attempt、lease、重试与终态均持久化并可恢复。
- [x] 初始任务在整批文件转正前保持不可执行的 `pending`；只在整批就绪后原子切换 `queued`。
- [x] 阶段完成和下一阶段激活在单一事务中编排，提交后才投递；投递丢失由扫描器恢复。
- [x] 删除流程以 `attempt_id` 为 fencing token，在持久化写入事务中校验 attempt/task 仍运行且资料未删除；超时由租约扫描器强制收敛。
- [x] 可相信回答、证据不足拒答、当前版本过滤和引用快照均有明确契约及功能测试。
- [x] 密码、刷新令牌、模型凭证、外发数据、脱敏和日志白名单边界已定义；模型调用只经过内部出口网关。
- [x] 前后端通过版本化 REST/SSE 契约协作，路由、服务、仓储、基础设施和 worker 职责分离。
- [x] 没有需要在复杂度跟踪中豁免的宪章偏离。

## Project Structure

### Documentation

```text
specs/001-orionamesh-rag-mvp/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── openapi.yaml
│   └── model-egress.md
└── tasks.md
```

### Source Code

```text
backend/
├── app/
│   ├── api/v1/
│   ├── core/
│   ├── db/
│   ├── infrastructure/{model_gateway,rate_limit,storage}/
│   ├── models/
│   ├── repositories/
│   ├── services/
│   └── workers/
├── migrations/versions/
└── tests/{unit,integration,contract,architecture,security}/

frontend/
├── app/
├── src/{components,features,lib}
└── tests/{unit,component,e2e}/

scripts/
deploy/
.github/workflows/
```

**结构决策**：采用后端与前端独立目录。业务层只能依赖 `model_gateway` 端口，供应商 SDK 和
凭证仅允许出现在 `infrastructure/model_gateway`；限流实现位于 `infrastructure/rate_limit`。
`chunks` 的检索、引用活表读取和流水线校验统一收口到 `backend/app/repositories/chunks.py`；
所有流水线持久化仓储接收 `attempt_id` 并执行 fencing 校验。

## 关键设计决策

### 1. 分阶段实施

1. **A1 后端工程基础**：uv、配置、日志、trace_id、数据库/Redis、迁移与 OpenAPI 基础校验。
2. **A2 后端认证与授权**：用户、持久化会话、JWT、刷新轮换、租户守卫、分级限流。
3. **A3 后端知识库与资料**：CRUD、整批上传、补偿、幂等、本地持久卷、Document/Task 状态。
4. **A4 后端异步流水线**：安全解析、处理名额、阶段编排、fencing、任务恢复、模型网关。
5. **A5 后端 RAG 与对话**：双路召回、RRF、可选 reranker、Context Pack、可信拒答、引用、SSE。
6. **A6 工程化与后端交付**：pnpm 前端骨架、Docker、CI，以及后端架构、迁移、契约和容器验证。
7. **B1 前端基础**：Next.js、API 客户端、认证状态、错误映射、Pino 脱敏。
8. **B2 前端业务渲染**：知识库、资料状态、会话、SSE 增量、引用展示。
9. **B3 联调与交付**：Playwright、Docker Compose、CI/CD、运维和回滚说明。

SC-001～SC-007 不映射为自动化量化门禁，不建立固定评测集、用户比例断言或性能阈值阻断；
相关核心行为通过对应 FR 的确定性测试验证。SC-008 继续作为模型出口安全自动化门禁。

### 2. 请求限流

- 注册和登录按来源 IP（20 次/5 分钟）及规范化邮箱 HMAC 摘要（5 次/5 分钟）限制。
- 刷新请求按来源 IP 及 refresh token HMAC 指纹使用相同阈值；上传按用户 10 次/10 分钟，问答按用户 20 次/分钟，其他认证端点按用户 120 次/分钟。
- Redis 原子计数只保存不可逆键；超限返回 `10005/429` 与 `Retry-After`，不产生业务副作用。
- Redis 不可用时状态变更端点返回 `50001/503`；只读 GET 是否 fail-open 由配置决定。

### 3. 模型出口网关

- Embedding、Query Rewrite、Reranker、回答生成全部通过内部网关。
- 供应商、端点、凭证和四类模型由 `MODEL_GATEWAY_*` 配置选择；Reranker 未配置时禁用并回退 RRF。
- 网关集中完成凭证注入、脱敏、超时、重试、降级、熔断和元数据审计；脱敏失败 fail-closed，日志不记录正文。

### 4. API 与生命周期契约

- 非 SSE 响应统一使用 `code/data/msg/trace_id`；所有操作声明 `50000/500`。
- Citation 详情和 SSE 使用同一 DTO；`live` 必须返回两个来源 ID，`snapshot` 必须使两个 ID 为空。
- 登出请求同时携带 Bearer Access Token 和 `refresh_token` 请求体；会话撤销以 `auth_sessions.revoked_at` 为真相源。
- 上传 `202` 的每个项目只能是 `queued`，或同步文件转正失败后的 `failed/20011`。

### 5. 上传、幂等与恢复边界

- 整批预校验后写临时对象；数据库事务为每批生成 `upload_batch_id`，创建资料、不可执行 `pending` 初始任务和可选幂等记录。
- 上传协调以 `DOCUMENT_UPLOAD_PENDING_TIMEOUT_SECONDS=300` 为超时边界。协调器按 `upload_batch_id` 对整批 documents 执行 `SELECT ... FOR UPDATE SKIP LOCKED`，在持有该短事务行锁期间完成同卷原子重命名、更新每项活动时间并最终切换批次状态；重放和恢复扫描器获取不到锁时不并发协调，只有获取锁后复查仍超时的 pending 批次可被接管。进程崩溃会释放行锁，已移动对象由幂等协调逻辑识别。
- 同幂等键重放若已收敛则返回首次结果；若同一批次仍在协调且未超时，返回 `20008/409`，不创建新副作用；若已超时，重放请求可使用与扫描器相同的幂等协调逻辑接管。
- 全部对象转正后，单一事务把整批资料、任务和幂等快照切为 `queued`；失败时整批补偿为 `failed/20011`。提交后投递，投递丢失由扫描器恢复。

### 6. 阶段编排与 fencing

- `parse → chunk → embed → finalize` 的阶段完成统一调用 `DocumentPipelineOrchestrator.complete_stage(attempt_id, result)`。
- 该事务锁定 attempt、task、document 与 processing lease，校验 attempt/task 为 `running`、资料未进入 `deleting/deleted` 且版本一致；随后把当前 attempt/task 标为 `succeeded`，幂等创建或激活下一任务，更新 `documents.current_task_type` 与 `lease.task_id`，最后提交。只有提交后才向 Celery 投递下一任务。
- 所有解析结果、草稿片段、正式 `chunks`、checkpoint 和阶段结果引用的写入仓储都必须携带 `attempt_id`；校验与写入位于同一数据库事务。fencing 失败时禁止写入，worker 将当前执行收敛为取消，不得绕过仓储重试。
- `embed` 依逻辑唯一键直写正式 `chunks`；`finalize` 只经 `ChunkRepository` 校验数量和版本并翻转完成状态。

### 7. 资料与知识库删除编排

- 资料删除事务先标记 `deleting`、取消未开始任务并幂等创建专用 `delete_cleanup` 任务。从提交起列表、详情和检索均隐藏资料；旧版本 `cleanup` 不承担删除职责。
- 存在运行 attempt 时不无限等待，也不提前释放其 lease：删除事务锁定 lease 并以当时的 `expires_at`（默认最长 300 秒）冻结等待上限；资料进入 deleting 后，心跳事务不得再续租。worker 下一个持久化边界会被 fencing 拦截并主动取消；若 worker 失联，孤儿任务扫描器到期后在事务中将 attempt/task 置 `cancelled`、释放 lease，再激活 `delete_cleanup`。
- `delete_cleanup` 删除原始对象、解析结果、草稿和正式片段后保留 `deleted` 墓碑；历史引用外键置空并保留非空快照。
- 删除知识库先标记内部状态 `deleting`，对其全部资料执行同一删除编排并立即从 API 隐藏。所有资料清理完成且无活动 attempt 后，维护扫描器才物理删除知识库，并级联对话、消息与引用；空知识库可在删除事务内直接删除。禁止依赖数据库立即级联来替代文件和 worker 协调。

## Phase 0：研究输出

[research.md](./research.md) 记录后端优先、数据库真相源、上传协调、阶段编排、fencing、有界删除、
知识库编排删除、失联恢复、安全解析、处理并发、本地卷、可信检索、引用 DTO、SSE、令牌、模型
配置、工具链、限流、模型出口、数据脱敏和日志白名单等已确定决策。

## Phase 1：设计输出

- [data-model.md](./data-model.md)：领域核心字段、关系、状态机、批次协调、阶段编排、fencing 和数据边界；ORM 与 Alembic 迁移是物理建表真相源。
- [contracts/openapi.yaml](./contracts/openapi.yaml)：版本化 REST/SSE、统一信封、分页、上传 202 收敛项、会话撤销、Citation 条件契约、错误码与限流。
- [quickstart.md](./quickstart.md)：uv/pnpm、扩展、配置、迁移、确定性功能/安全测试、Docker 与 CI 验证路径。

## Post-Design Constitution Check

设计后复核仍全部通过。上传批次、阶段切换、运行 attempt、处理名额和删除接管均由 PostgreSQL
记录及事务驱动；fencing 保证删除提交后运行 worker 无法再写入。超时扫描能使 pending、running、
deleting 收敛到明确状态。SC-001～SC-007 的产品指标不会替代宪章要求，也不会被误用为自动化门禁。

## Complexity Tracking

无宪章偏离，无需记录例外。
