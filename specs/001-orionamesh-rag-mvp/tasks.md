---
description: "OrionaMesh 个人知识库 RAG MVP 的后端优先实施任务"
---

# 任务：OrionaMesh 个人知识库 RAG MVP

**输入**：[plan.md](./plan.md)、[spec.md](./spec.md)、[research.md](./research.md)、
[data-model.md](./data-model.md)、[openapi.yaml](./contracts/openapi.yaml)、
[model-egress.md](./contracts/model-egress.md)、[quickstart.md](./quickstart.md)

**策略**：严格执行 Backend-First。阶段 1–7 完成后端业务逻辑、工程化基线、REST/SSE 契约与
A5/A6 门禁；阶段 8–10 才允许开发前端。前端不得直接访问数据库、队列或文件存储，且不得在客户端复制
授权、状态机或业务错误判断规则。

**测试**：本功能的计划与快速验证明确要求 API 契约、集成、权限、状态机、检索和 SSE 验证。
用户故事与高风险业务逻辑的测试按先写失败测试、再实现的顺序安排；项目骨架、日志、响应工厂等
基础设施任务采用实现后的契约验证，以确认其对外行为符合冻结契约。

**成功指标边界**：SC-001～SC-007 仅作为产品成功指标，不属于自动化测试、阶段门禁或发布
阻断条件；任务不得创建固定评测集、比例断言或性能阈值门禁。相关功能行为仍按 FR 和用户故事
进行确定性测试。SC-008 的模型出口安全验证继续作为自动化门禁。

## 格式：`[ID] [P?] [用户故事] 描述`

- `[P]`：可与同阶段其他任务并行（不同文件、无未完成依赖）。
- `[US#]`：任务归属用户故事；基础、门禁与跨切面任务不带用户故事标签。
- 所有任务均含精确文件路径。

## 阶段 1：后端项目与通用基础设施

**目的**：建立后端项目、测试框架、数据库迁移、可观测性与统一响应基础；本阶段未实现用户故事。

- [ ] T001 创建后端项目清单、Python 3.12 依赖与开发命令于 `backend/pyproject.toml`
- [ ] T002 配置 uv 项目依赖、开发依赖组、Python 版本与锁文件策略于 `backend/pyproject.toml`、`backend/uv.lock`
- [ ] T003 配置 Ruff 格式化/检查、Pyright 类型检查与 pytest 命令于 `backend/pyproject.toml`
- [ ] T004 [P] 创建 FastAPI 应用入口、配置加载与 `/health` 健康检查于 `backend/app/main.py`、`backend/app/core/config.py`
- [ ] T005 [P] 配置 pytest 单元、集成和契约测试发现规则于 `backend/pyproject.toml`、`backend/tests/conftest.py`
- [ ] T006 配置 SQLAlchemy 会话、Alembic 与 PostgreSQL 连接于 `backend/app/infrastructure/database/session.py`、`backend/alembic.ini`、`backend/migrations/env.py`
- [ ] T007 创建启用 `vector`、`pg_trgm` 与 UUID 扩展的初始迁移于 `backend/migrations/versions/0001_extensions.py`
- [ ] T008 [P] 实现 structlog JSON 配置及 password/token/secret_key 递归脱敏处理于 `backend/app/core/logging.py`
- [ ] T009 [P] 编写结构化日志脱敏单元测试于 `backend/tests/unit/core/test_logging.py`
- [ ] T010 实现生成或透传 UUID `trace_id` 的请求中间件于 `backend/app/api/middleware/trace.py`
- [ ] T011 实现统一 `ApiEnvelope`、异常映射和 `code/data/msg/trace_id` 响应工厂于 `backend/app/api/v1/schemas/common.py`、`backend/app/api/middleware/errors.py`
- [ ] T012 [P] 编写统一响应、trace_id 和非 SSE 错误信封契约测试于 `backend/tests/contract/test_api_envelope.py`
- [ ] T013 [P] 编写 uv 锁文件、Ruff、Pyright 与后端质量命令可执行性检查于 `scripts/check-backend.sh`、`backend/tests/unit/test_toolchain.py`

**检查点**：后端能启动；扩展、结构化日志、trace_id 和 JSON 信封可独立验证。

---

## 阶段 2：后端基础领域、身份与租户边界（阻塞全部用户故事）

**目的**：建立数据模型、租户范围仓储、JWT 会话、知识库边界、分级限流和统一模型出口；后续业务只能通过这些服务与内部基础设施访问数据或外部模型。此阶段覆盖 FR-026～FR-029 与 SC-008 的共享实现。

**独立验证**：认证、上传、问答和普通接口使用正确限流策略；跨实例计数一致且超限无业务副作用；四类模型调用只能经过网关，脱敏失败时假供应商收到零请求，日志只含白名单元数据。

- [ ] T014 创建用户（`last_login_at` 可空）、登录会话、带 `active/deleting` 内部状态的知识库、资料（含内部 `upload_batch_id`）、任务/尝试（attempt ID 为 fencing token，区分旧版本 `cleanup` 与删除 `delete_cleanup`）、上传幂等记录（含批次及 coordinating/accepted/failed）、处理并发名额、片段、对话、消息和引用 ORM 模型于 `backend/app/models/`
- [ ] T015 创建实体关系、独立资料/任务/消息状态枚举、资料 `upload_batch_id` 索引、`20001/20010～20014/50000` 异步 `error_code` 约束、上传幂等唯一键、资料级处理名额约束、同一任务最多一个未结束 attempt 的部分唯一约束及租户索引迁移；强制 `users.last_login_at NULL`、`conversations.knowledge_base_id NOT NULL ON DELETE CASCADE`、`messages.user_id`、`message_citations.user_id/knowledge_base_id` 及其 `rank/score/chunk_snapshot NOT NULL`、`rank >= 1`、`UNIQUE(message_id, rank)`，于 `backend/migrations/versions/0002_domain_schema.py`
- [ ] T016 [P] 创建 Redis 连接、队列配置和连接健康检查于 `backend/app/core/redis.py`、`backend/app/core/readiness.py`
- [ ] T017 [P] 实现请求密码校验、密码哈希与 JWT 编解码（Access 2 小时、Refresh 7 天）于 `backend/app/core/security.py`
- [ ] T018 [P] 编写密码哈希、JWT 时长与敏感令牌不落库单元测试于 `backend/tests/unit/core/test_security.py`
- [ ] T019 实现当前用户认证依赖、Bearer 解析和 `10001/401` 过期令牌错误于 `backend/app/api/v1/dependencies/auth.py`
- [ ] T020 实现以 `user_id` 为强制范围的用户、知识库、资料、任务、对话和引用仓储基类，并建立唯一 `ChunkRepository`：检索方法固定过滤用户/知识库/完成态/当前版本，流水线方法固定过滤用户/知识库/资料/精确版本，于 `backend/app/repositories/base.py`、`backend/app/repositories/chunks.py`
- [ ] T021 [P] 编写跨用户资源拒绝、不泄露内容、未 finalize/旧版本片段排除及流水线精确版本计数的仓储集成测试于 `backend/tests/integration/repositories/test_tenant_scope.py`、`backend/tests/integration/repositories/test_chunk_repository.py`
- [ ] T022 实现认证、当前用户与知识库资源路由并注册 `/v1` 路由器于 `backend/app/api/v1/routes/auth.py`、`backend/app/api/v1/routes/users.py`、`backend/app/api/v1/routes/knowledge_bases.py`、`backend/app/api/v1/router.py`
- [ ] T023 实现用户、会话和知识库服务：注册保持 `last_login_at=NULL`、登录后更新；登出以 Bearer 用户 + 请求体 refresh token 定位并幂等写 `auth_sessions.revoked_at`，已撤销/过期且属于当前用户时仍成功，无法匹配/跨用户映射 `10006/401`；知识库删除先置 `deleting`、复用资料删除编排，全部资料清理且无活动 attempt 后才物理删除并级联对话/消息/引用，空知识库可立即删除，于 `backend/app/services/auth_service.py`、`backend/app/services/user_service.py`、`backend/app/services/knowledge_base_service.py`
- [ ] T024 [P] 实现根 Pydantic Settings 装配、SecretStr 类型、环境前缀和生产环境必填配置校验于 `backend/app/core/settings.py`
- [ ] T025 [P] 编写注册后 `last_login_at` 为空、登录后更新、刷新、Bearer + refresh 请求体登出并持久化撤销/重复删除幂等/跨用户拒绝、用户资料和知识库 CRUD 的 API 契约测试；覆盖知识库 `page/page_size`、删除后立即隐藏且清理完成前不提前级联、刷新令牌失效 `10006/401`、刷新限流使用 token HMAC 指纹且不记录原值与 2 小时 Access Token，于 `backend/tests/contract/test_auth_api.py`、`backend/tests/contract/test_knowledge_bases_api.py`
- [ ] T026 [P] 实现限流阈值/窗口、主体 HMAC 密钥、只读 fail-open，资料持久卷/解析限制/每用户处理名额/300 秒处理 lease/`DOCUMENT_UPLOAD_PENDING_TIMEOUT_SECONDS=300`/上传幂等保留期，以及模型网关供应商/端点/密钥/脱敏策略/禁止 payload 审计的 Pydantic 配置与就绪校验于 `backend/app/infrastructure/rate_limit/config.py`、`backend/app/infrastructure/storage/config.py`、`backend/app/infrastructure/model_gateway/config.py`、`backend/app/core/readiness.py`
- [ ] T027 [P] 实现注册/登录的 IP+规范化邮箱、刷新的 IP+refresh token HMAC 指纹、当前用户的不可逆限流键和四类端点策略注册表；禁止 Redis/日志保存原始 token，于 `backend/app/infrastructure/rate_limit/keys.py`、`backend/app/infrastructure/rate_limit/policies.py`
- [ ] T028 实现 Redis 原子滑动窗口、TTL 清理、跨实例共享计数和 `Retry-After` 计算于 `backend/app/infrastructure/rate_limit/redis_limiter.py`、`backend/app/infrastructure/rate_limit/scripts/sliding_window.lua`
- [ ] T029 实现 FastAPI 限流中间件/依赖及 `10005/429 RATE_LIMIT_EXCEEDED`、`50001/503 RATE_LIMIT_PROTECTION_UNAVAILABLE` 统一信封；全部状态变更 fail-closed、只读按配置降级且拒绝发生在业务写入前，于 `backend/app/api/middleware/rate_limit.py`、`backend/app/api/middleware/errors.py`
- [ ] T030 [P] 编写注册/登录 IP+邮箱、刷新 IP+token HMAC 指纹双重阈值、上传/问答/默认策略、`Retry-After`、原始 token 不进 Redis/日志、零业务副作用和 Redis 故障语义测试于 `backend/tests/contract/test_rate_limits.py`、`backend/tests/integration/infrastructure/test_redis_rate_limiter.py`
- [ ] T031 [P] 定义 `ModelGateway` 协议、`ModelCall`/`SanitizedModelCall`、四类调用输入输出及供应商适配器边界于 `backend/app/infrastructure/model_gateway/types.py`、`backend/app/infrastructure/model_gateway/gateway.py`、`backend/app/infrastructure/model_gateway/providers/base.py`
- [ ] T032 实现最小数据选择、禁止字段移除、邮箱/电话/证件号不可逆占位符、策略版本和脱敏异常 fail-closed 于 `backend/app/infrastructure/model_gateway/sanitizer.py`、`backend/app/infrastructure/model_gateway/policies/v1.py`
- [ ] T033 [P] 实现严格调用元数据白名单、供应商错误分类及禁止请求/响应/脱敏正文进入日志的审计器于 `backend/app/infrastructure/model_gateway/audit.py`、`backend/app/core/logging.py`
- [ ] T034 实现配置驱动的供应商/四类模型路由和仅在发送边界注入凭证的 provider 工厂；使用 Quickstart 固定的 `MODEL_GATEWAY_*_MODEL/*_TIMEOUT_SECONDS/*_MAX_RETRIES`，Reranker 模型为空时禁用且不影响就绪，于 `backend/app/infrastructure/model_gateway/providers/factory.py`、`backend/app/infrastructure/model_gateway/providers/langchain.py`
- [ ] T035 实现统一模型出口编排，将脱敏、路由、凭证、各调用类型超时/重试/降级和元数据审计串联，且脱敏失败不得创建外部请求，于 `backend/app/infrastructure/model_gateway/service.py`
- [ ] T036 [P] 编写四类调用最小化、禁止字段脱敏、占位符稳定性、fail-closed、发送边界凭证注入和日志白名单单元测试于 `backend/tests/unit/infrastructure/model_gateway/test_sanitizer.py`、`backend/tests/unit/infrastructure/model_gateway/test_gateway.py`、`backend/tests/unit/infrastructure/model_gateway/test_audit.py`

**检查点**：迁移后所有共享实体可用；任何非当前用户资源访问在服务端被拒绝；JWT、会话与知识库 REST 接口返回统一信封；分级限流和模型出口已成为 API/worker 可复用且不可旁路的后端基础设施。

---

## 阶段 3：用户故事 1 — 建立并维护私有知识库（后端）

**目标**：让已认证用户创建知识库、批量上传受限资料并从持久化状态了解异步处理结果。

**独立测试**：用户可创建知识库、上传一份有效资料，收到即时接受状态并最终看到 `completed` 或 `failed`；第二用户无法读取或操作这些资料。

### 先写测试

- [ ] T037 [P] [US1] 编写 PDF/DOCX/MD/TXT、`20009/400` 不支持格式、`20003/400` 单文件 50MB、`20004/400` 单次 20 文件、整批任一失败则全部无副作用和统一错误信封的上传 API 契约测试于 `backend/tests/contract/test_documents_api.py`
- [ ] T038 [P] [US1] 编写上传批次协调/补偿与 202 契约测试：数据库失败清临时对象；数据库提交至文件转正期间 `pending` 任务不可执行；全部转正后整批资料/任务/幂等快照原子 `queued`；任一转正失败三者 `failed/20011`、对象全清且零 parse 投递；断言 `202` 每项只能为 queued 或 failed/20011，并覆盖跨用户拒绝和详情 HTTP `200` 持久化失败码，于 `backend/tests/integration/documents/test_upload_and_access.py`、`backend/tests/contract/test_documents_api.py`
- [ ] T039 [P] [US1] 编写四类解析器、解析器版本、空/扫描资料 `20010`、损坏资料 `20001`、宏/脚本/外链禁用、路径穿越、压缩炸弹、解压大小和超时防护的失败测试于 `backend/tests/unit/services/parsers/test_document_parsers.py`、`backend/tests/integration/documents/test_parse_security.py`
- [ ] T040 [P] [US1] 编写重复内容创建独立资料、同一 `Idempotency-Key` 重放不重复创建、同键不同请求冲突、未超时 coordinating 重放 `20008/409` 且零副作用、超过 300 秒后由重放或扫描器锁定接管、批次成功/补偿后快照分别返回 `queued`/`failed/20011`、24 小时保留及过期清理的失败测试于 `backend/tests/integration/documents/test_upload_idempotency.py`
- [ ] T041 [P] [US1] 编写单用户最多 3 个资料级处理名额、跨阶段持续持有、事务竞争，以及失联 `running` 任务原子关闭活动 attempt/释放 lease/恢复 queued 或失败且不存在双活动 attempt 的失败测试于 `backend/tests/integration/documents/test_processing_concurrency.py`
- [ ] T042 [P] [US1] 编写流水线事务编排和 fencing 失败测试：当前 attempt/task 成功、下一阶段幂等创建、`current_task_type`、`lease.task_id` 同事务一致，提交后才投递；解析结果/草稿/chunks/checkpoint 写入均携带 `attempt_id` 并同事务校验 attempt/task running、版本一致、document 非 deleting；另覆盖 embed 直写、finalize 只校验/翻转、发布前不可检索、数量不一致 `20013` 与稳定失败码，于 `backend/tests/integration/documents/test_pipeline_state_machine.py`
- [ ] T043 [P] [US1] 编写 Celery 投递失败后的 queued 幂等重投、超过 300 秒且复查仍超时的 pending 上传批次按 `upload_batch_id` 锁定接管、失联 running 的 attempt/lease/任务事务恢复、deleting 后心跳不得续租、到冻结 expires_at 后强制 cancelled 并激活 delete_cleanup、running 无活动 lease 立即接管、重试耗尽及过期幂等清理的失败测试于 `backend/tests/integration/documents/test_task_recovery.py`
- [ ] T044 [P] [US1] 编写嵌入统一网关、配置覆盖、维度校验、超时重试、脱敏失败零外发和资料终态的失败测试于 `backend/tests/unit/services/llm/test_embeddings.py`

### 后端实现

- [ ] T045 [US1] 实现整批上传格式、文件大小和数量的无副作用前置校验；任一失败整批拒绝，不支持格式映射为 `20009/400 UNSUPPORTED_FILE_TYPE`，文件超限映射为 `20003/400 FILE_TOO_LARGE`，数量超限映射为 `20004/400 TOO_MANY_FILES`，于 `backend/app/services/upload_validation.py`
- [ ] T046 [US1] 实现以 `/data/orionamesh` 为默认根目录的本地持久卷适配器、相对对象键、路径逃逸防护，以及按 `upload_batch_id/document_id` 可推导、可检查、可幂等转正和整批清理的临时/正式对象接口，于 `backend/app/infrastructure/storage/local.py`、`backend/app/services/file_storage.py`
- [ ] T047 [US1] 在整批预校验和临时写入后以内部 `upload_batch_id` 原子创建全部 pending 资料、不可执行初始 parse 任务与 coordinating 幂等记录；通过 `SELECT FOR UPDATE SKIP LOCKED` 锁定批次并在短事务中完成同卷原子重命名/活动时间更新；同键未超时重放返回 `20008/409`，超时重放可锁定并调用同一协调函数；全部转正后原子更新三者为 queued 再投递并返回仅含 queued 的 `202`，失败则补偿为 `failed/20011`，数据库失败清对象并返回 `50000/500`，于 `backend/app/services/document_service.py`、`backend/app/repositories/upload_requests.py`
- [ ] T048 [US1] 实现资料列表、详情、任务详情和删除 REST 路由于 `backend/app/api/v1/routes/documents.py`
- [ ] T049 [US1] 实现资料上传、详情、`page/page_size/status` 列表和任务响应模式；Document/DocumentTask DTO 的可空 `error_code` 限定为 `20001/20010～20014/50000`，并区分 `202` 同步接受与 HTTP `200` 异步失败详情，于 `backend/app/api/v1/schemas/documents.py`
- [ ] T050 [US1] 实现 Celery 应用、提交后投递适配层和恢复/维护扫描器：普通执行只重投 queued；按 `upload_batch_id` 锁定并复查超过 300 秒的 pending 批次后调用共享幂等协调函数；对 lease 过期 running 事务性关闭 attempt/释放 lease并按预算恢复或失败，对 deleting 资料则 cancelled 并激活 delete_cleanup；完成所有 deleted 子资料后物理删除 deleting 知识库；禁止双活动 attempt并清理过期幂等记录，于 `backend/app/workers/celery_app.py`、`backend/app/workers/task_recovery.py`、`backend/app/workers/base.py`
- [ ] T051 [US1] 实现 PyMuPDF、python-docx、markdown-it-py、charset-normalizer 解析适配器及统一安全包装层；外部解析后以 `attempt_id` fencing 仓储事务写解析结果，空文本持久化 `20010`，损坏/不可解析持久化 `20001`，于 `backend/app/services/parsers/`、`backend/app/workers/document_parse.py`、`backend/app/repositories/parse_results.py`
- [ ] T052 [US1] 实现数据库事务型资料级 `document_processing_leases`：首次进入 processing 获取、跨 parse/chunk/embed/finalize 持有并更新当前 task 归属、默认每用户 3 个、心跳续租、终态释放和恢复扫描器失联回收，于 `backend/app/repositories/processing_leases.py`、`backend/app/workers/task_recovery.py`
- [ ] T053 [US1] 实现 `chunk` 阶段：生成仅中间可见的草稿片段，并以 `attempt_id` fencing 仓储事务写入版本/租户元数据于 `backend/app/workers/document_chunk.py`、`backend/app/repositories/chunk_drafts.py`
- [ ] T054 [US1] 实现 `embed` 阶段：外部调用不持有数据库事务；取得向量后经 `ChunkRepository` 以 `attempt_id` fencing 在同一事务校验并按唯一逻辑键批量直写正式 `chunks`/checkpoint，支持重试安全批次和失败 `20012`，不得翻转资料为 completed，于 `backend/app/workers/document_embed.py`、`backend/app/repositories/chunks.py`、`backend/app/repositories/document_tasks.py`
- [ ] T055 [US1] 实现只依赖内部 `ModelGateway` 的嵌入用例适配器、默认 `text-embedding-3-small`（1536 维）、30 秒超时和 2 次指数退避重试于 `backend/app/services/llm/embeddings.py`
- [ ] T056 [US1] 实现 `DocumentPipelineOrchestrator` 与 `finalize/cleanup`：统一事务锁定并校验 attempt/task/document/lease，完成当前阶段、幂等创建或激活下一阶段、更新 `current_task_type`/`lease.task_id`，提交后才投递；finalize 只经 `ChunkRepository` 校验并原子翻转 completed/chunk_count/释放 lease，不一致持久化 `20013`；cleanup 只清理旧版本，于 `backend/app/services/document_pipeline.py`、`backend/app/repositories/document_tasks.py`、`backend/app/workers/document_finalize.py`、`backend/app/workers/document_cleanup.py`
- [ ] T057 [US1] 实现资料删除语义：事务置 `deleting`、取消未开始任务、幂等创建专用 `delete_cleanup` 并立即隐藏；无活动 attempt 时释放 lease/激活清理，有活动 attempt 时锁定并以当时 `expires_at` 冻结上限且禁止心跳续租，无活动 lease 则立即接管；实现 delete_cleanup 清理原始对象与全部派生数据、置空引用外键但保留必填快照、保留不可查询 `deleted` 墓碑，GET 返回 404，于 `backend/app/services/document_deletion_service.py`、`backend/app/workers/document_delete_cleanup.py`
- [ ] T058 [P] [US1] 运行并修复四类解析、上传事务/幂等、处理并发、嵌入网关和流水线状态集成测试于 `backend/tests/unit/services/parsers/`、`backend/tests/integration/documents/test_upload_and_access.py`、`backend/tests/integration/documents/test_upload_idempotency.py`、`backend/tests/integration/documents/test_processing_concurrency.py`、`backend/tests/integration/documents/test_pipeline_state_machine.py`
- [ ] T059 [P] [US1] 运行并修复 Celery 投递失败后扫描器幂等重投递、失联处理名额回收、过期上传幂等记录清理和重试耗尽稳定错误码测试于 `backend/tests/integration/documents/test_task_recovery.py`

**检查点**：资料处理由持久化任务记录驱动；失败资料只显示失败原因与删除操作；MVP 无重处理或替换接口。

---

## 阶段 4：用户故事 2 — 基于资料进行可信连续问答（后端）

**目标**：让用户在已授权知识库中进行带来源引用的连续问答；没有证据时明确拒答。

**独立测试**：对已完成资料提问可获得 SSE 回答和引用；不相关问题、无完成资料、其他用户资料、旧版本或未完成资料均不能成为回答证据。

### 先写测试

- [ ] T060 [P] [US2] 编写会话 CRUD/分页/消息状态和统一 Citation DTO 契约测试：`live` 强制两个 UUID、`snapshot` 强制两个 ID 为 null、定位/内容、rank 顺序和页码分页；覆盖知识库完成编排清理后的级联删除及跨用户拒绝，于 `backend/tests/contract/test_conversations_api.py`
- [ ] T061 [P] [US2] 编写向量/关键词双路检索只能通过统一 `ChunkRepository` 且强制用户、知识库、版本、完成状态过滤，以及 RRF 的失败测试于 `backend/tests/integration/retrieval/test_tenant_version_filters.py`、`backend/tests/unit/services/test_retrieval.py`
- [ ] T062 [P] [US2] 编写无完成资料和无证据可信拒答的失败测试于 `backend/tests/unit/services/test_answer_rejection.py`
- [ ] T063 [P] [US2] 编写原始 SSE 文本帧、五类判别事件、`retrieval_done` 与详情复用 Citation 字段语义，以及断开后固定持久化 `cancelled` 的失败测试于 `backend/tests/contract/test_messages_sse_api.py`、`backend/tests/integration/conversations/test_sse_cancellation.py`
- [ ] T064 [P] [US2] 编写改写、reranker 和生成网关配置、超时、重试、降级与脱敏失败零外发的失败测试于 `backend/tests/unit/services/llm/test_resilience.py`

### 后端实现

- [ ] T065 [US2] 实现必须绑定当前用户知识库的会话及消息仓储/服务于 `backend/app/services/conversation_service.py`、`backend/app/repositories/conversations.py`
- [ ] T066 [US2] 实现会话 CRUD、可空标题/最后消息时间、会话/引用页码分页和消息游标分页路由/判别模式，强制 user 消息为 completed、assistant 消息为 streaming 或明确终态；知识库在资料清理完成后才级联对话/消息/引用，资料删除保留引用快照，于 `backend/app/api/v1/routes/conversations.py`、`backend/app/api/v1/schemas/conversations.py`
- [ ] T067 [US2] 在统一 `ChunkRepository` 中实现向量召回并强制 `user_id`、知识库、当前版本、`completed` 与 documents join 过滤，于 `backend/app/repositories/chunks.py`
- [ ] T068 [US2] 在同一 `ChunkRepository` 中实现 pg_trgm 关键词召回并复用相同租户/版本/完成状态过滤构造器，于 `backend/app/repositories/chunks.py`
- [ ] T069 [US2] 实现 RRF、可选 reranker 降级、3000 token 上下文打包和相邻片段去重于 `backend/app/services/retrieval_service.py`
- [ ] T070 [US2] 实现只依赖内部 `ModelGateway` 的可选 reranker 用例适配器、10 秒超时、1 次重试和 RRF 直接回退于 `backend/app/services/llm/reranker.py`
- [ ] T071 [US2] 实现只依赖内部 `ModelGateway` 的查询改写/生成用例适配器、最近三轮最小上下文、改写 10 秒/1 次原问题回退、生成首 token 15 秒/总时长 120 秒/1 次重试、无证据拒答和 `20005/409 KNOWLEDGE_BASE_NOT_READY` 于 `backend/app/services/answer_service.py`、`backend/app/services/llm/chat.py`
- [ ] T072 [US2] 实现统一 Citation DTO：当前来源返回 `source_type=live`，删除/不可访问来源将 ID 置空并从快照返回 `source_type=snapshot`、文件类型、定位和内容预览，按 rank 排序，于 `backend/app/services/citation_service.py`
- [ ] T073 [US2] 按 OpenAPI 的文本线格式与 `x-sse-event-schema` 判别联合实现五类统一信封事件，并在断连后固定持久化 `cancelled` 于 `backend/app/api/v1/sse/message_stream.py`
- [ ] T074 [US2] 将消息发送路由接入检索、生成、引用和 SSE 流于 `backend/app/api/v1/routes/messages.py`
- [ ] T075 [P] [US2] 运行并修复确定性的 RRF、有证据回答保存字段完整 Citation、无证据拒答和删除后 snapshot 功能测试于 `backend/tests/unit/services/test_retrieval.py`、`backend/tests/unit/services/test_answer_rejection.py`、`backend/tests/unit/services/test_citations.py`
- [ ] T076 [P] [US2] 运行并修复 SSE 原始帧、解码判别事件及断开后 assistant 消息 `cancelled` 的契约/集成测试于 `backend/tests/contract/test_messages_sse_api.py`、`backend/tests/integration/conversations/test_sse_cancellation.py`
- [ ] T077 [P] [US2] 运行并修复改写、reranker 和生成全部经网关、配置选择、超时、重试、脱敏失败回退及最终 `cancelled` 测试于 `backend/tests/unit/services/llm/test_resilience.py`

**检查点**：所有回答仅以当前用户、知识库、已完成当前版本资料为证据；SSE 事件使用统一信封；纯聊天模式不存在。

---

## 阶段 5：用户故事 3 — 掌握资料处理与异常结果（后端）

**目标**：让用户诊断资料处理并安全删除资料，同时维持历史回答的来源快照。

**独立测试**：用户可查看资料和任务的明确终态与失败原因；删除资料后新问答排除其内容，历史回答仅显示不可恢复的快照。

### 先写测试

- [ ] T078 [P] [US3] 编写资料/任务各自状态枚举、任务阶段、尝试记录结构、持久化 `error_code`、失败原因仅对所有者可见和失败后无重处理操作的 API 契约测试于 `backend/tests/contract/test_document_status_api.py`
- [ ] T079 [P] [US3] 编写 DELETE 后立即隐藏、delete_cleanup 后 `deleted` 墓碑与 GET 404、无运行 attempt 时立即接管、运行 attempt 写入被事务 fencing 拒绝、等待不超过 lease.expires_at、超时扫描 cancelled/释放/激活清理、知识库两阶段删除且无孤儿文件、检索排除、必填引用快照及明确终态测试于 `backend/tests/integration/documents/test_deletion_and_citations.py`、`backend/tests/integration/documents/test_terminal_states.py`、`backend/tests/integration/knowledge_bases/test_deletion_orchestration.py`

### 后端实现

- [ ] T080 [US3] 完善资料与任务详情服务的阶段、尝试、`20001/20010～20014/50000` 错误分类、失败原因和安全错误摘要映射于 `backend/app/services/document_status_service.py`
- [ ] T081 [US3] 完善资料/知识库删除编排、有界 lease 等待、孤儿 attempt 安全接管、delete_cleanup、处理名额释放与非空引用快照规则，禁止恢复原始资料访问于 `backend/app/services/document_deletion_service.py`、`backend/app/services/knowledge_base_service.py`、`backend/app/services/citation_service.py`、`backend/app/workers/task_recovery.py`
- [ ] T082 [US3] 在资料/任务详情响应中暴露终态、契约限定的持久化失败码、失败原因和唯一允许的删除操作标识于 `backend/app/api/v1/schemas/documents.py`
- [ ] T083 [P] [US3] 运行并修复“宁可明确失败、不展示无限处理中或 deleting”、删除 fencing、有界超时接管与知识库清理编排集成测试于 `backend/tests/integration/documents/test_terminal_states.py`、`backend/tests/integration/documents/test_deletion_and_citations.py`、`backend/tests/integration/knowledge_bases/test_deletion_orchestration.py`

**检查点**：用户不会看到无结束处理状态；失败资料没有重处理/替换入口；历史引用保留快照但不暴露已删除原文。

---

## 阶段 6：A5 完成后的后端契约冻结门禁

**目的**：冻结后端业务规则与 `/v1` REST/SSE 契约。此阶段不开发前端功能。

- [ ] T084 使实现与 OpenAPI 契约中的 Bearer+refresh 登出、202 仅 queued 或 failed/20011、DocumentTaskType 的 delete_cleanup、完整 DTO、Citation live/snapshot 条件约束、异步持久化错误、上传 pending 超时/幂等、消息判别状态、可空字段、限流和 SSE 判别事件一致于 `specs/001-orionamesh-rag-mvp/contracts/openapi.yaml`、`backend/app/api/v1/`
- [ ] T085 [P] 运行并修复迁移、pgvector/pg_trgm、本地持久卷可写与处理名额配置就绪检查于 `backend/tests/integration/test_startup_readiness.py`
- [ ] T086 [P] 运行并修复 `10006/401`、`20009/400`、异步 `20001/20010～20014` 固定安全提示、`10005/429`、`50001/503` 及所有非 SSE 操作的 `50000/500` 统一信封与 trace_id 契约测试于 `backend/tests/contract/test_business_error_codes.py`、`backend/tests/contract/test_rate_limits.py`
- [ ] T087 [P] 运行并修复认证会话撤销、租户隔离、未就绪 pending 不执行/300 秒上传接管/幂等、解析安全、阶段事务编排、attempt fencing、资料级处理名额、有界资料/知识库删除、embed 直写/finalize 发布、检索过滤、非空引用快照与 SSE 取消全链路测试于 `backend/tests/integration/test_backend_gate.py`
- [ ] T088 [P] 编写架构依赖测试：禁止供应商旁路；禁止路由/服务/worker 直接读取 chunks；要求解析结果/草稿/chunks/checkpoint 的写仓储签名包含 attempt_id 且复用统一 fencing guard；禁止 delete_cleanup 与旧版本 cleanup 混用；验证层次依赖、Redis/Celery 不作为任务/名额真相源及业务层不拼本地绝对路径，于 `backend/tests/architecture/test_model_gateway_boundaries.py`、`backend/tests/architecture/test_chunk_repository_boundaries.py`、`backend/tests/architecture/test_pipeline_fencing_boundaries.py`、`backend/tests/architecture/test_layer_boundaries.py`、`backend/tests/architecture/test_task_truth_source.py`、`backend/tests/architecture/test_storage_boundaries.py`
- [ ] T089 [P] 使用受控假供应商验证四类调用全部经网关、脱敏失败零网络请求、凭证边界、超时重试降级和日志白名单于 `backend/tests/integration/infrastructure/test_model_egress.py`
- [ ] T090 [P] 校验内部模型出口契约与实现的四类调用、最小字段、脱敏状态、provider 前置条件和日志白名单一致于 `specs/001-orionamesh-rag-mvp/contracts/model-egress.md`、`backend/tests/contract/test_model_egress_contract.py`
- [ ] T091 冻结后端 OpenAPI/SSE/内部模型出口契约并记录限流、出口安全与“A5 实现完成后的契约门禁”通过结果于 `specs/001-orionamesh-rag-mvp/contracts/openapi.yaml`、`specs/001-orionamesh-rag-mvp/contracts/model-egress.md`、`specs/001-orionamesh-rag-mvp/quickstart.md`

**检查点**：A5 实现及其契约冻结门禁通过后，后端是唯一可信业务实现；全部外部模型调用无旁路、脱敏失败无外发、日志无 payload，限流错误和故障语义已冻结；仍需完成 A6 工程化门禁后才允许开始前端 UI 或联调任务。

---

## 阶段 7：工程化、部署与 A6 交付门禁（前端开始前必须完成）

**目的**：把所有技术栈固定为可复现、可校验和可部署的工程基线。该阶段不得开发前端 UI。

- [ ] T092 创建 pnpm 根工作区、Node 版本、共享脚本及唯一根锁文件策略于 `package.json`、`pnpm-workspace.yaml`、`pnpm-lock.yaml`、`.nvmrc`、`frontend/package.json`
- [ ] T093 配置前端 pnpm 项目、Next.js/React/TypeScript/Tailwind/Shadcn/UI/Pino 依赖及开发命令于 `frontend/package.json`、`frontend/next.config.ts`、`frontend/src/app/layout.tsx`
- [ ] T094 [P] 配置 ESLint、Prettier、TypeScript 严格检查和 Vitest 于 `frontend/eslint.config.mjs`、`frontend/.prettierrc.json`、`frontend/tsconfig.json`、`frontend/vitest.config.ts`、`frontend/tests/setup.ts`
- [ ] T095 [P] 配置 Pino 仅用于 Next.js 服务端日志，并过滤 token、密码、资料内容和引用快照于 `frontend/src/lib/logging/server.ts`
- [ ] T096 [P] 按 Quickstart 固定变量名创建限流、持久卷/解析/并发、300 秒 processing lease、300 秒 upload pending 超时、幂等、四类模型名及各调用超时重试环境模板；Query Rewrite/Generation 必填、Embedding 默认、Reranker 空值禁用，于 `.env.example`、`backend/.env.example`、`frontend/.env.example`
- [ ] T097 创建后端多阶段 Docker 镜像并用 uv 锁定安装 PyMuPDF、python-docx、markdown-it-py、charset-normalizer 等依赖于 `deploy/docker/backend.Dockerfile`
- [ ] T098 [P] 创建前端多阶段 Docker 镜像并用根 pnpm 锁文件锁定安装与构建于 `deploy/docker/frontend.Dockerfile`
- [ ] T099 创建 PostgreSQL、Redis、后端 API、Celery worker 和前端的本地 Docker Compose 编排，为 API/worker 共同挂载 `/data/orionamesh` 命名持久卷并验证共用网关/限流配置于 `deploy/compose/compose.yaml`
- [ ] T100 [P] 创建后端质量、迁移（含 last_login 可空、Citation 非空/唯一、delete_cleanup 与知识库 deleting）、扩展、OpenAPI、SSE、解析依赖、本地卷、限流和模型网关配置校验脚本于 `scripts/check-backend.sh`、`scripts/verify-contracts.sh`
- [ ] T101 [P] 创建前端 pnpm lint、format、类型检查、单测和端到端校验脚本于 `scripts/check-frontend.sh`
- [ ] T102 创建 GitHub Actions PR CI：uv/根 pnpm 锁定安装、Ruff、Pyright、ESLint、Prettier、类型检查、单元/集成/契约/架构测试、迁移与契约校验于 `.github/workflows/ci.yml`
- [ ] T103 创建 GitHub Actions 受保护分支镜像构建、漏洞扫描和发布工作流于 `.github/workflows/image.yml`
- [ ] T104 [P] 编写 Docker Compose 健康/就绪、容器重建后持久卷保留、锁文件不可变安装及配置失败冒烟测试于 `backend/tests/integration/test_delivery_stack.py`
- [ ] T105 运行 A6 工具链、Compose、持久卷重建、阶段编排/fencing/删除接管、CI workflow、限流/出口安全门禁与镜像构建验证，并冻结环境变量和交付命令于 `README.md`、`specs/001-orionamesh-rag-mvp/quickstart.md`

**检查点**：uv、pnpm、全部质量工具、Docker/Compose、本地持久卷、CI/CD、可配置模型网关、Redis 限流与出口安全已验证；T105 后才允许前端 UI。

---

## 阶段 8：前端基础与用户故事 1 渲染（仅在 T105 后）

**目标**：基于冻结契约实现认证、知识库和资料上传/状态展示，不在前端复制后端规则。

**独立测试**：用户可在浏览器中登录、创建知识库、上传受限资料、轮询最终状态并删除失败资料。

### 先写测试

- [ ] T106 [P] [US1] 编写 API 客户端按 `code` 处理同步错误和详情 HTTP `200` 内异步 `error_code`、上传 `Idempotency-Key`、分页、trace_id 与限流的失败测试于 `frontend/tests/unit/lib/api/client.test.ts`
- [ ] T107 [P] [US1] 编写认证、知识库列表、上传限制/重放、轮询终态、空文档失败删除、`allowed_actions` 和权限提示组件失败测试于 `frontend/tests/component/user-story-1.test.tsx`

### 前端实现

- [ ] T108 [US1] 实现 API 信封、Bearer 会话及携带 refresh token 请求体的登出、trace_id、SSE、上传幂等键/协调中 409、分页、同步业务码和异步资源 `error_code` 客户端封装于 `frontend/src/lib/api/client.ts`、`frontend/src/lib/api/types.ts`
- [ ] T109 [US1] 实现登录、注册、会话恢复和受保护路由于 `frontend/src/app/(auth)/login/page.tsx`、`frontend/src/app/(auth)/register/page.tsx`、`frontend/src/features/auth/`
- [ ] T110 [US1] 实现知识库页码列表、创建、编辑和删除渲染于 `frontend/src/app/knowledge-bases/page.tsx`、`frontend/src/features/knowledge-bases/`
- [ ] T111 [US1] 实现资料批量选择/拖放、50MB/20 文件提示、请求级幂等键和上传进度渲染于 `frontend/src/features/documents/UploadPanel.tsx`
- [ ] T112 [US1] 实现资料页码/状态列表、完整 DTO 详情轮询、`20001/20010～20014/50000` 固定安全提示及 `allowed_actions` 仅删除操作渲染于 `frontend/src/features/documents/DocumentList.tsx`、`frontend/src/features/documents/DocumentDetail.tsx`

**检查点**：前端只消费阶段 6 冻结的后端接口；资料失败不显示重处理/替换；所有错误以 `code/msg/trace_id` 表现。

---

## 阶段 9：前端用户故事 2 渲染（仅在 T105 后）

**目标**：渲染必须绑定知识库的会话、历史消息、SSE 回答、拒答提示和来源引用。

**独立测试**：用户可选择知识库创建对话、看到流式回答和引用；无证据与取消均有明确反馈。

### 先写测试

- [ ] T113 [P] [US2] 编写知识库绑定、可空会话标题、分页、五类 SSE 增量、无证据、取消和引用快照组件失败测试于 `frontend/tests/component/user-story-2.test.tsx`

### 前端实现

- [ ] T114 [US2] 实现知识库绑定会话页码列表、可空标题展示、创建、重命名与删除界面于 `frontend/src/app/conversations/page.tsx`、`frontend/src/features/conversations/ConversationList.tsx`
- [ ] T115 [US2] 实现消息历史分页、提问和五类判别式 SSE 信封事件解析于 `frontend/src/features/conversations/MessageThread.tsx`、`frontend/src/features/conversations/useMessageStream.ts`
- [ ] T116 [US2] 实现无完成资料/无证据、`cancelled`、Token 过期、无权限、限流与 trace_id 用户提示于 `frontend/src/features/conversations/ConversationFeedback.tsx`
- [ ] T117 [US2] 按统一 Citation DTO 实现引用页码按需加载、rank 顺序、`source_type=live` 当前来源与 ID 为空的 `snapshot` 快照抽屉，于 `frontend/src/features/citations/CitationDrawer.tsx`

**检查点**：前端不允许创建纯聊天；SSE 流与失败都按统一信封解析；引用快照不可恢复原始资料。

---

## 阶段 10：前端用户故事 3 渲染与端到端联调（仅在 T105 后）

**目标**：完成处理诊断、删除体验和浏览器端的端到端质量门禁。

**独立测试**：资料处理失败或中断时用户可理解终态和失败原因；删除后新问答排除资料，历史引用仍显示快照。

### 先写测试与替身

- [ ] T118 [P] [US3] 编写完整 DocumentTask DTO、异步失败码、失败终态、删除确认与不可见重处理入口组件失败测试于 `frontend/tests/component/user-story-3.test.tsx`
- [ ] T119 [US3] 配置前后端契约替身与浏览器端到端测试环境于 `frontend/tests/e2e/fixtures/api.ts`、`frontend/playwright.config.ts`
- [ ] T120 [US3] 编写认证、上传重放、限流、轮询、空文档失败删除、对话、SSE、引用快照与跨用户反馈端到端失败测试于 `frontend/tests/e2e/orionamesh-mvp.spec.ts`

### 前端实现

- [ ] T121 [US3] 实现完整 DocumentTask DTO 的尝试、进度、处理阶段、持久化失败码、失败原因和删除确认渲染于 `frontend/src/features/documents/TaskHistory.tsx`、`frontend/src/features/documents/DeleteDocumentDialog.tsx`

**检查点**：浏览器端完整主路径通过；前端未绕过后端授权、状态机或契约。

---

## 阶段 11：文档与跨切面收尾

- [ ] T122 [P] 更新开发环境、uv/根 pnpm 锁文件、持久卷、解析器、处理并发、上传幂等、限流与模型网关说明于 `README.md`
- [ ] T123 [P] 执行并记录快速验证清单的最终结果于 `specs/001-orionamesh-rag-mvp/quickstart.md`
- [ ] T124 审查普通日志、模型调用审计、响应、SSE 和引用快照，确认不含 password/token/secret_key、请求/响应 payload、提示词、问题、片段、文件名、请求头或已删除原始资料于 `backend/app/core/logging.py`、`backend/app/infrastructure/model_gateway/audit.py`、`frontend/src/lib/logging/server.ts`、`backend/tests/integration/test_backend_gate.py`
- [ ] T125 运行全部确定性后端与前端测试、迁移、OpenAPI/模型出口契约、上传超时接管、阶段编排、写入 fencing、有界资料/知识库删除、解析安全、处理并发、持久卷、架构边界、限流/出口安全、质量工具与 Compose 验证，并记录结果于 `specs/001-orionamesh-rag-mvp/quickstart.md`

---

## 依赖与执行顺序

```text
阶段 1（后端基础）
  → 阶段 2（身份与租户边界）
    → 阶段 3（US1 后端：入库闭环）
      → 阶段 4（US2 后端：可信问答）
        → 阶段 5（US3 后端：诊断与删除）
          → 阶段 6（A5 后端契约门禁）
            → 阶段 7（A6 工程化、部署与交付门禁）
              → 阶段 8（US1 前端）
                → 阶段 9（US2 前端）
                  → 阶段 10（US3 前端与联调）
                    → 阶段 11（收尾）
```

- **T091 是 A5 门禁**：T092–T105 不得在其通过前开始；**T105 是 A6 门禁**：T106–T121 前端与联调任务不得在其通过前开始。
- **US1 后端**依赖阶段 1–2；**US2 后端**依赖 US1 已产生可检索完成资料；**US3 后端**依赖 US1 的状态机与删除基础。
- 前端用户故事沿用后端顺序，以避免消费未冻结 API；阶段 8–10 的 `[P]` 测试可与不同组件实现并行。

## 并行机会

- 阶段 1：T004、T005、T008、T009、T012、T013 可在依赖完成后分文件并行。
- 阶段 2：T016/T018、T020/T021、T026/T027/T031/T033 可在各自前置完成后分文件并行；T030、T036 可分别验证限流和网关实现。
- US1：T037–T044 可按契约、解析、幂等、并发、流水线、恢复与嵌入分文件并行编写；实现完成后并行运行 T058/T059。
- US2：T060–T064 可按会话、检索、拒答、SSE 与模型弹性分文件并行；T075–T077 在对应实现后并行。
- US3：T078/T079 可并行；T083 在状态与删除实现后执行。
- 工程化：T094–T098、T100/T101、T103/T104 可按不同文件并行。
- 前端仅在 T105 后：US1 的 T106/T107、US2 的 T113、US3 的 T118 可先独立建立失败测试。

## 用户故事并行执行示例

### US1：私有知识库与资料入库

```text
并行启动 T037：上传格式、大小、数量和错误码契约失败测试
并行启动 T038–T044：分别覆盖上传事务、解析安全、请求幂等、处理并发、流水线、恢复和嵌入网关
随后按 T045 → T057 实现，最后并行运行 T058 与 T059
```

### US2：可信连续问答

```text
并行启动 T060：会话、分页、可空字段、编排清理后级联删除与租户契约失败测试
并行启动 T061/T062：双路检索、RRF 与可信拒答失败测试
并行启动 T063/T064：SSE 线格式、取消与模型弹性失败测试
随后实现 T065 → T074，最后并行运行 T075、T076、T077
```

### US3：处理诊断与删除

```text
并行启动 T078：资料/任务 DTO、持久化错误码与失败操作契约失败测试
并行启动 T079：删除、名额释放、检索排除、引用快照和终态收敛失败测试
随后实现 T080 → T082，最后运行 T083
```

## 实施策略

### 后端 MVP 先行

1. 完成阶段 1 与 2。
2. 实现并验证阶段 3 的资料入库闭环。
3. 实现并验证阶段 4 的可信问答。
4. 完成阶段 5 的诊断与删除语义。
5. 通过阶段 6 的 A5 门禁，冻结 REST/SSE 契约。
6. 完成阶段 7 的 A6 工具链、部署与 CI/CD 门禁。
7. 在此之前禁止创建前端 UI、联调或绕过后端的临时逻辑。

**建议首个可演示 MVP 范围**：完成 T001–T059，可独立演示认证、私有知识库、受限批量上传、
安全解析、请求幂等、处理并发、持久卷、明确终态、任务恢复、限流与嵌入出口安全；完整 RAG 产品
MVP 仍需继续完成 T060–T105 后再进入前端。

### 前端增量交付

1. T105 后先完成阶段 8，演示认证、知识库、上传和资料状态。
2. 继续阶段 9，演示绑定知识库的流式可信问答与来源。
3. 完成阶段 10 和 11，进行端到端验证与文档收尾。

## 备注

- `[P]` 仅表示不同文件且不依赖同一未完成输出；不改变 T091/T105 的后端先行门禁。
- 任务 T001–T091 为后端或后端验证任务；T092–T105 为工程化、部署与 CI/CD 任务；
  T106–T121 为前端或前后端联调任务。
- 不实现资料重处理、资料替换、纯聊天和自助密码重置；这些需求不得在任务执行中重新引入。
