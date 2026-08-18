---
description: "OrionaMesh 个人知识库 RAG MVP 的后端优先实施任务"
---

# 任务：OrionaMesh 个人知识库 RAG MVP

**输入**：[plan.md](./plan.md)、[spec.md](./spec.md)、[research.md](./research.md)、
[data-model.md](./data-model.md)、[openapi.yaml](./contracts/openapi.yaml)、
[model-egress.md](./contracts/model-egress.md)、[quickstart.md](./quickstart.md)

> 文档职责：本文件只定义实施顺序、交付物和验证任务；不重新定义需求、数据状态机、API、错误码或配置默认值。实现发生变更时，先更新对应权威文档，再同步受影响任务。

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

- [x] T001 创建后端项目清单、Python 3.12 依赖、LangChain 的 OpenAI-compatible 协议集成依赖与开发命令于 `backend/pyproject.toml`
- [x] T002 配置 uv 项目依赖、开发依赖组、Python 版本与锁文件策略于 `backend/pyproject.toml`、`backend/uv.lock`
- [x] T003 配置 Ruff 格式化/检查、Pyright 类型检查与 pytest 命令于 `backend/pyproject.toml`
- [x] T004 [P] 创建 FastAPI 应用入口，从唯一根配置模块加载设置并实现 `/health` 健康检查于 `backend/app/main.py`、`backend/app/core/settings.py`
- [x] T005 [P] 配置 pytest 单元、集成和契约测试发现规则于 `backend/pyproject.toml`、`backend/tests/conftest.py`
- [x] T006 配置 SQLAlchemy 会话、Alembic 与 PostgreSQL 连接于 `backend/app/infrastructure/database/session.py`、`backend/alembic.ini`、`backend/migrations/env.py`
- [x] T007 创建启用 `vector`、`pg_trgm` 与 UUID 扩展的初始迁移于 `backend/migrations/versions/0001_extensions.py`
- [x] T008 [P] 实现 structlog JSON 配置及 password/token/secret_key 递归脱敏处理于 `backend/app/core/logging.py`
- [x] T009 [P] 编写结构化日志脱敏单元测试于 `backend/tests/unit/core/test_logging.py`
- [x] T010 实现生成或透传 UUID `trace_id` 的请求中间件于 `backend/app/api/middleware/trace.py`
- [x] T011 实现统一 `ApiEnvelope`、异常映射和 `code/data/msg/trace_id` 响应工厂于 `backend/app/api/v1/schemas/common.py`、`backend/app/api/middleware/errors.py`
- [x] T012 [P] 编写统一响应、trace_id 和非 SSE 错误信封契约测试于 `backend/tests/contract/test_api_envelope.py`
- [x] T013 [P] 编写 uv 锁文件、Ruff、Pyright 与后端质量命令可执行性检查于 `scripts/check-backend.sh`、`backend/tests/unit/test_toolchain.py`

**检查点**：后端能启动；扩展、结构化日志、trace_id 和 JSON 信封可独立验证。

---

## 阶段 2：后端基础领域、身份与租户边界（阻塞全部用户故事）

**目的**：建立数据模型、租户范围仓储、JWT 会话、知识库边界、分级限流和统一模型出口；后续业务只能通过这些服务与内部基础设施访问数据或外部模型。此阶段覆盖 FR-026～FR-029 与 SC-008 的共享实现。

**独立验证**：认证、上传、问答和普通接口使用正确限流策略；跨实例计数一致且超限无业务副作用；四类模型调用只能经过网关，脱敏失败时假供应商收到零请求，日志只含白名单元数据。

- [x] T014 创建用户（`last_login_at` 可空；email 只保存单一规范化函数结果）、登录会话、带 `active/deleting/delete_failed` 与 `delete_error_code` 的知识库、资料（含内部 `upload_batch_id`、`delete_cycle` 及仅镜像当前任务的 `retry_count`）、任务/尝试（后台任务初次 `attempt_no=1/retry_count=0`，所有类型 `max_retries=3` 表示初次执行外最多重试 3 次、单任务最多 4 个 attempt，每个新任务独立计数且与模型网关重试独立；任务保存 `delete_cycle`；attempt ID 为 fencing token；attempt 创建时写入非空 `started_at`，并保存 worker、可空结束/错误/耗时；attempt 冗余父任务的 `user_id/knowledge_base_id/document_id/document_version` 且不可变，并声明 `(task_id, user_id, knowledge_base_id, document_id, document_version)` 到父任务同序五列的复合外键；创建时锁定并校验父任务；区分旧版本 `cleanup` 与删除 `delete_cleanup`）、上传幂等记录（含批次及 coordinating/accepted/failed）、处理并发名额、片段、对话、消息和引用 ORM 模型于 `backend/app/models/`
- [x] T015 创建实体关系、独立资料/任务/消息状态枚举、后台文档任务 `retry_count DEFAULT 0` 与 `max_retries DEFAULT 3`、知识库 `active/deleting/delete_failed` 与 `delete_error_code=20015` 配对约束、资料 `upload_batch_id` 索引、`20001/20010～20015/50000` 异步 `error_code` 约束、`delete_cycle` 及 delete_cleanup 约束、上传幂等唯一键、资料级处理名额约束、同一任务最多一个未结束 attempt 的部分唯一约束及租户索引迁移；建立任务与 attempt 租户边界复合外键并保留诊断索引；强制 users.email 规范化后唯一和 `last_login_at NULL`、对话知识库级联、消息用户边界、assistant 状态/结束原因配对及 Citation 非空/排名唯一约束，于 `backend/migrations/versions/0002_domain_schema.py`
- [x] T016 [P] 创建 Redis 连接、队列配置和连接健康检查于 `backend/app/core/redis.py`、`backend/app/core/readiness.py`
- [x] T017 [P] 实现请求密码校验、唯一邮箱规范化函数（去除首尾 Unicode 空白、格式校验后完整值 Unicode `casefold`）、密码哈希，以及固定 HS256 的 2 小时 Access Token JWT 编解码：只接受 `sub/iat/exp/type=access` 且不得按 token 头动态选择算法；实现 7 天 Refresh Token：32 字节 CSPRNG、无填充 Base64URL、`rt_` 前缀、总长 46，并以 SHA-256 摘要验证；Refresh Token 不得编码为 JWT，于 `backend/app/core/security.py`
- [x] T018 [P] 编写邮箱规范化存储/登录/限流复用、密码哈希、HS256 白名单、`sub/iat/exp/type=access`、2 小时 Access JWT 时长、缺失/格式错误/签名或算法无效/缺失声明/type 错误/过期统一 `10001/401` 拒绝，以及 Refresh Token 格式/随机性/非 JWT/仅 SHA-256 摘要落库与敏感令牌不落日志单元测试于 `backend/tests/unit/core/test_security.py`
- [x] T019 实现当前用户认证依赖、Bearer 解析和 Access Token 缺失、格式错误、签名或算法无效、必填声明/type 错误及过期统一 `10001/401` 错误于 `backend/app/api/v1/dependencies/auth.py`
- [x] T020 实现以 `user_id` 为强制范围的用户、知识库、资料、任务、任务尝试、对话和引用仓储基类，并建立唯一 `ChunkRepository`：按 ID 查询在当前用户范围内未命中时不得全局探测，知识库映射 `20002/404`、其他资源映射 `20007/404`；任务尝试仓储只允许事务锁定父任务后复制/校验冗余边界，依赖数据库复合外键作为最后一道一致性约束并安全转换完整性异常，读取固定过滤当前用户；检索方法固定过滤用户/知识库/完成态/当前版本，流水线方法固定过滤用户/知识库/资料/精确版本，于 `backend/app/repositories/base.py`、`backend/app/repositories/document_task_attempts.py`、`backend/app/repositories/chunks.py`
- [x] T021 [P] 编写跨用户知识库统一 `20002/404`、其他跨用户资源（含任务尝试）统一 `20007/404`、禁止全局存在性探测和不泄露内容测试；覆盖 attempt 父任务边界字段不一致拒绝（以已有 `task_id` 写入任一不匹配的用户/知识库/资料/版本边界时必须由数据库复合外键拒绝）、未 finalize/旧版本片段排除及流水线精确版本计数，于 `backend/tests/integration/repositories/test_tenant_scope.py`、`backend/tests/integration/repositories/test_document_task_attempts.py`、`backend/tests/integration/repositories/test_chunk_repository.py`
- [x] T022 实现认证、当前用户与知识库资源路由并注册 `/v1` 路由器于 `backend/app/api/v1/routes/auth.py`、`backend/app/api/v1/routes/users.py`、`backend/app/api/v1/routes/knowledge_bases.py`、`backend/app/api/v1/router.py`
- [x] T023 实现用户、会话和知识库基础服务：注册和登录均复用 T017 邮箱规范化函数，注册保持 `last_login_at=NULL`、登录后更新；刷新轮换在同一数据库事务中按摘要锁定旧 session 并复查有效性，只允许一个并发请求撤销旧会话并创建单一后继，后到请求返回 `10006/401`；刷新令牌无效、过期、已撤销或重放时只拒绝本次刷新，不得撤销该用户其他 active sessions；登出以 Bearer 用户 + 请求体 refresh token 定位并幂等写 `auth_sessions.revoked_at`，已撤销/过期且属于当前用户时仍成功，无法匹配/跨用户映射 `10006/401`；实现知识库创建、查询、更新和空知识库的同步删除，非空知识库的 `deleting` 编排留待 T081（依赖资料删除能力）实现，于 `backend/app/services/auth_service.py`、`backend/app/services/user_service.py`、`backend/app/services/knowledge_base_service.py`
- [x] T024 [P] 在唯一根配置模块实现 Pydantic Settings 装配、SecretStr 类型、环境变量前缀和统一必填配置启动校验；按 `APP_ENV` 选择环境文件（`development`→`.env.local`、`test`→`.env.test`、`staging`/`production`→不读仓库文件由 Docker/CI 注入），缺少关键变量时启动直接失败，部署环境拒绝回退默认密码/开发密钥/本地存储路径（见 quickstart 环境契约）；只暴露必填 `AUTH_JWT_SECRET_KEY`，要求 UTF-8 编码后至少 32 字节且不得与限流/供应商凭证复用，算法 `HS256` 与 TTL 7200 秒保留为不可配置的安全代码常量；项目不得再创建并行的 `core/config.py` 配置真相源，于 `backend/app/core/settings.py`、`backend/app/core/readiness.py`
- [x] T025 [P] 编写注册后 `last_login_at` 为空、登录后更新、HS256 Access JWT 的必填声明/固定 2 小时 TTL、随机不透明 Refresh Token、事务锁定刷新轮换及同 token 并发恰好一成功/单一后继/后到 `10006/401`、无效/过期/撤销/重放不撤销其他 active sessions、Bearer + refresh 请求体登出并持久化撤销/重复删除幂等/跨用户拒绝、用户资料和知识库创建/查询/更新及空知识库同步删除的 API 契约与集成测试；覆盖知识库 `page/page_size`、跨租户知识库 `20002/404` 且无全局探测、`delete_failed` 禁止 PATCH 并返回 `20008/409`、刷新限流使用 `refresh-ip-and-token` 策略和 token HMAC 指纹且不记录原值；非空知识库删除失败墓碑和异步清理契约由 T079/T081 覆盖，于 `backend/tests/contract/test_auth_api.py`、`backend/tests/integration/auth/test_refresh_rotation.py`、`backend/tests/contract/test_knowledge_bases_api.py`
- [x] T026 [P] 实现限流阈值/窗口、主体 HMAC 密钥、只读 fail-open、可信代理，资料持久卷/解析限制/处理名额/lease/上传幂等，检索 `RETRIEVAL_VECTOR_MIN_SIMILARITY=0.65`/`RETRIEVAL_TRGM_MIN_SIMILARITY=0.30` 和 `MESSAGE_STREAMING_STALE_SECONDS=360`（两个阈值在 `[0,1]`，消息上限不小于模型最大尝试预算加 60 秒），以及模型网关 `openai-compatible` provider、必填 endpoint、密钥、脱敏策略和禁止 payload 审计的 Pydantic 配置与就绪校验；endpoint 无默认值且必须为合法 HTTPS base URL，仅本地开发/自动化测试允许主机名精确为 `localhost` 或回环 IP `127.0.0.1`/`::1` 的 HTTP endpoint，其他 HTTP、缺失/非法 endpoint 或未知 provider 必须拒绝就绪，于 `backend/app/infrastructure/rate_limit/config.py`、`backend/app/infrastructure/storage/config.py`、`backend/app/infrastructure/model_gateway/config.py`、`backend/app/services/retrieval_config.py`、`backend/app/core/readiness.py`
- [x] T027 [P] 实现可信来源 IP 解析器：默认忽略全部转发头并使用直连对端，仅当直连对端命中可信代理 CIDR 时解析 `X-Forwarded-For`、由右向左选择首个非可信地址，非法链回退直连对端且不信任 `X-Real-IP`；再实现注册/登录的 `auth-ip-and-account`（来源 IP+T017 规范化邮箱 HMAC）、刷新的 `refresh-ip-and-token`（来源 IP+refresh token HMAC 指纹）、当前用户策略的不可逆限流键和端点策略注册表；禁止 Redis/日志保存原始 token 或完整转发链，于 `backend/app/infrastructure/rate_limit/source_ip.py`、`backend/app/infrastructure/rate_limit/keys.py`、`backend/app/infrastructure/rate_limit/policies.py`
- [x] T028 实现 Redis 原子滑动窗口、TTL 清理、跨实例共享计数和 `Retry-After` 计算于 `backend/app/infrastructure/rate_limit/redis_limiter.py`、`backend/app/infrastructure/rate_limit/scripts/sliding_window.lua`
- [x] T029 实现 FastAPI 限流中间件/依赖及 `10005/429 RATE_LIMIT_EXCEEDED`、`50001/503 RATE_LIMIT_PROTECTION_UNAVAILABLE` 统一信封；全部状态变更 fail-closed、只读按配置降级且拒绝发生在业务写入前，于 `backend/app/api/middleware/rate_limit.py`、`backend/app/api/middleware/errors.py`
- [x] T030 [P] 编写注册/登录 IP+邮箱、刷新 IP+token HMAC 指纹双重阈值、上传/问答/默认策略、`Retry-After`、原始 token 不进 Redis/日志、零业务副作用和 Redis 故障语义测试；覆盖默认伪造 XFF 不生效、可信代理多跳由右向左取首个非可信地址、非法链或全可信链回退直连对端、完整转发链不进日志/Redis，于 `backend/tests/contract/test_rate_limits.py`、`backend/tests/unit/infrastructure/rate_limit/test_source_ip.py`、`backend/tests/integration/infrastructure/test_redis_rate_limiter.py`
- [x] T031 [P] 定义 `ModelGateway` 协议、`ModelCall`/`SanitizedModelCall`、四类调用输入输出及供应商适配器边界；Reranker 输出固定为完整 `scores` 数组，每项含零基 `candidate_index` 与有限数值 `score`，于 `backend/app/infrastructure/model_gateway/types.py`、`backend/app/infrastructure/model_gateway/gateway.py`、`backend/app/infrastructure/model_gateway/providers/base.py`
- [x] T032 实现最小数据选择、禁止字段移除、邮箱/电话/证件号不可逆占位符、策略版本和脱敏异常 fail-closed 于 `backend/app/infrastructure/model_gateway/sanitizer.py`、`backend/app/infrastructure/model_gateway/policies/v1.py`
- [x] T033 [P] 实现严格调用元数据白名单、供应商错误分类及禁止请求/响应/脱敏正文进入日志的审计器于 `backend/app/infrastructure/model_gateway/audit.py`、`backend/app/core/logging.py`
- [x] T034 实现配置驱动的 provider 工厂和 MVP `openai-compatible` LangChain 适配器，仅在发送边界注入凭证且每次调用只执行一次物理供应商请求，禁用 LangChain/底层客户端内建重试，由网关传入本次 timeout 并负责跨尝试编排；Embedding 使用 embeddings 端点，Query Rewrite/Generation 使用 chat 端点，可选 Reranker 通过 chat 端点返回 `scores[{candidate_index,score}]`，严格拒绝缺失、重复/越界序号和非有限 score，任何解析/校验失败必须作为整次重排失败且不得返回部分评分；endpoint、模型及超时重试预算由 Quickstart 配置，未知 provider 启动失败，Reranker 为空时禁用，于 `backend/app/infrastructure/model_gateway/providers/factory.py`、`backend/app/infrastructure/model_gateway/providers/openai_compatible.py`
- [x] T035 实现统一模型出口编排，将脱敏、路由、凭证、各调用类型超时/重试、稳定失败分类和元数据审计串联；作为模型调用超时与重试的唯一执行者，对业务调用方只返回最终成功或失败结果，且脱敏失败不得创建外部请求；不得在网关实现领域降级，于 `backend/app/infrastructure/model_gateway/service.py`
- [x] T036 [P] 编写四类调用最小化、禁止字段脱敏、占位符稳定性、fail-closed、`openai-compatible` 路由/发送边界凭证注入、供应商适配器单次物理请求且仅网关按配置预算重试（断言无业务层/LangChain 隐式额外请求）、HTTPS endpoint 与本机回环 HTTP 例外、其他 HTTP/缺失/非法 endpoint 与未知 provider 拒绝就绪、网关最终失败分类不执行领域降级、Reranker 完整评分/非法响应整体回退和日志白名单单元测试于 `backend/tests/unit/infrastructure/model_gateway/test_sanitizer.py`、`backend/tests/unit/infrastructure/model_gateway/test_gateway.py`、`backend/tests/unit/infrastructure/model_gateway/test_audit.py`

**检查点**：迁移后所有共享实体可用；任何非当前用户资源访问在服务端被拒绝；JWT、会话与知识库 REST 接口返回统一信封；分级限流和模型出口已成为 API/worker 可复用且不可旁路的后端基础设施。

---

## 阶段 3：用户故事 1 — 建立并维护私有知识库（后端）

**目标**：让已认证用户创建知识库、批量上传受限资料并从持久化状态了解异步处理结果。

**独立测试**：用户可创建知识库、上传一份有效资料，收到即时接受状态并最终看到 `completed` 或 `failed`；第二用户无法读取或操作这些资料。

### 先写测试

- [x] T037 [P] [US1] 编写 PDF/DOCX/MD/TXT、`20009/400` 不支持格式、`20003/400` 单文件 50MB、`20004/400` 单次 20 文件、整批任一失败则全部无副作用和统一错误信封的上传 API 契约测试于 `backend/tests/contract/test_documents_api.py`
- [x] T038 [P] [US1] 编写上传批次协调/补偿与 202 契约测试：数据库失败清临时对象；数据库提交至文件转正期间 `pending` 任务不可执行；全部转正后整批资料/任务/幂等快照原子 `queued`；任一转正失败三者 `failed/20011`、对象全清且零 parse 投递；断言 `202` 每项只能为 queued 或 failed/20011，并覆盖跨用户资料统一 `20007/404` 且无全局探测和详情 HTTP `200` 持久化失败码，于 `backend/tests/integration/documents/test_upload_and_access.py`、`backend/tests/contract/test_documents_api.py`
- [x] T039 [P] [US1] 编写四类解析器、解析器版本、空/扫描资料 `20010`、损坏资料 `20001`、宏/脚本/外链禁用、路径穿越、压缩炸弹、解压大小和超时防护的失败测试于 `backend/tests/unit/services/parsers/test_document_parsers.py`、`backend/tests/integration/documents/test_parse_security.py`
- [x] T040 [P] [US1] 编写重复内容创建独立资料、同一 `Idempotency-Key` 重放不重复创建、同键不同请求冲突、未超时 coordinating 重放 `20008/409` 且零副作用、超过 300 秒后由重放或扫描器锁定接管、批次成功/补偿后快照分别返回 `queued`/`failed/20011`、24 小时保留及过期清理的失败测试于 `backend/tests/integration/documents/test_upload_idempotency.py`
- [x] T041 [P] [US1] 编写单用户最多 3 个资料级处理名额、跨阶段持续持有、事务竞争，以及失联 `running` 任务原子关闭活动 attempt/释放 lease/恢复 queued 或失败且不存在双活动 attempt 的失败测试于 `backend/tests/integration/documents/test_processing_concurrency.py`
- [x] T042 [P] [US1] 编写流水线事务编排和 fencing 失败测试：当前 attempt/task 成功、下一阶段幂等创建、`current_task_type`、`lease.task_id` 同事务一致，提交后才投递；解析结果/草稿/chunks/checkpoint 写入均携带 `attempt_id` 并同事务校验 attempt/task running、版本一致、document 非 deleting；另覆盖 embed 直写、finalize 只校验/翻转、发布前不可检索、数量不一致 `20013` 与稳定失败码，于 `backend/tests/integration/documents/test_pipeline_state_machine.py`
- [x] T043 [P] [US1] 编写 Celery 投递失败后的 queued 幂等重投、超过 300 秒且复查仍超时的 pending 上传批次锁定接管、后台任务初次 `attempt_no=1/retry_count=0`、`max_retries=3` 时最多 4 个 attempt/达到预算不再排队/每个新任务独立计数、非空 `started_at` 与完整 Attempt DTO 字段、文档计数镜像当前任务并在阶段切换/新删除轮次/完成时重置、与模型调用重试独立、失联 running 的 attempt/lease/任务事务恢复、deleting 后禁止续租/到期 cancelled 并激活 delete_cleanup、无活动 lease 立即接管及过期幂等清理测试于 `backend/tests/integration/documents/test_task_recovery.py`
- [x] T044 [P] [US1] 编写嵌入统一网关、配置覆盖、维度校验、超时重试、脱敏失败零外发和资料终态的失败测试于 `backend/tests/unit/services/llm/test_embeddings.py`

### 后端实现

- [x] T045 [US1] 实现整批上传格式、文件大小和数量的无副作用前置校验；任一失败整批拒绝，不支持格式映射为 `20009/400 UNSUPPORTED_FILE_TYPE`，文件超限映射为 `20003/400 FILE_TOO_LARGE`，数量超限映射为 `20004/400 TOO_MANY_FILES`，于 `backend/app/services/upload_validation.py`
- [x] T046 [US1] 实现以 `/data/orionamesh` 为默认根目录的本地持久卷适配器、相对对象键、路径逃逸防护，以及按 `upload_batch_id/document_id` 可推导、可检查、可幂等转正和整批清理的临时/正式对象接口，于 `backend/app/infrastructure/storage/local.py`、`backend/app/services/file_storage.py`
- [x] T047 [US1] 在整批预校验和临时写入后以内部 `upload_batch_id` 原子创建全部 pending 资料、不可执行初始 parse 任务与 coordinating 幂等记录；通过 `SELECT FOR UPDATE SKIP LOCKED` 锁定批次并在短事务中完成同卷原子重命名/活动时间更新；同键未超时重放返回 `20008/409`，超时重放可锁定并调用同一协调函数；全部转正后原子更新三者为 queued 再投递并返回仅含 queued 的 `202`，失败则补偿为 `failed/20011`，数据库失败清对象并返回 `50000/500`，于 `backend/app/services/document_service.py`、`backend/app/repositories/upload_requests.py`
- [x] T048 [US1] 实现资料列表、详情和任务详情 REST 路由；列表查询先固定排除内部 `deleting/deleted` 再应用公开 `status` 过滤，任何过滤参数不得绕过隐藏边界（删除路由依赖 T057 的删除编排），于 `backend/app/api/v1/routes/documents.py`
- [x] T049 [US1] 实现资料上传、详情、`page/page_size/status` 列表和任务响应模式；公开 Document 状态与过滤枚举仅允许 `pending/queued/processing/completed/failed`，传入 `deleting/deleted` 映射为 `10003/400`；Document/DocumentTask DTO 的可空 `error_code` 限定为 `20001/20010～20015/50000`，并区分 `202` 同步接受与 HTTP `200` 异步失败详情，于 `backend/app/api/v1/schemas/documents.py`
- [x] T050 [US1] 实现 Celery 应用、提交后投递适配层和恢复/维护扫描器：普通执行只重投 queued；按 `upload_batch_id` 锁定并复查超过 300 秒的 pending 批次后调用共享幂等协调函数；对 lease 过期且仍为 running 的正常资料事务性关闭 attempt/释放 lease并按预算恢复或失败；禁止双活动 attempt并清理过期幂等记录。deleting 资料的取消、delete_cleanup 激活及非空知识库的物理删除由依赖资料删除能力的 T057/T081 接入该扫描器；消息实现完成后 T074 将该扫描器扩展为条件收敛超时 `streaming` assistant 消息，于 `backend/app/workers/celery_app.py`、`backend/app/workers/task_recovery.py`、`backend/app/workers/base.py`
- [x] T051 [US1] 实现 PyMuPDF、python-docx、markdown-it-py、charset-normalizer 解析适配器及统一安全包装层；外部解析后以 `attempt_id` fencing 仓储事务写解析结果，空文本持久化 `20010`，损坏/不可解析持久化 `20001`，于 `backend/app/services/parsers/`、`backend/app/workers/document_parse.py`、`backend/app/repositories/parse_results.py`
- [x] T052 [US1] 实现数据库事务型资料级 `document_processing_leases`：首次进入 processing 获取、跨 parse/chunk/embed/finalize 持有并更新当前 task 归属、默认每用户 3 个、心跳续租、终态释放和恢复扫描器失联回收，于 `backend/app/repositories/processing_leases.py`、`backend/app/workers/task_recovery.py`
- [x] T053 [US1] 实现 `chunk` 阶段：生成仅中间可见的草稿片段，并以 `attempt_id` fencing 仓储事务写入版本/租户元数据于 `backend/app/workers/document_chunk.py`、`backend/app/repositories/chunk_drafts.py`
- [x] T054 [US1] 实现 `embed` 阶段：外部调用不持有数据库事务；取得向量后经 `ChunkRepository` 以 `attempt_id` fencing 在同一事务校验并按唯一逻辑键批量直写正式 `chunks`/checkpoint，支持重试安全批次和失败 `20012`，不得翻转资料为 completed，于 `backend/app/workers/document_embed.py`、`backend/app/repositories/chunks.py`、`backend/app/repositories/document_tasks.py`
- [x] T055 [US1] 实现只依赖内部 `ModelGateway` 的嵌入用例适配器：声明 embedding 调用类型、默认 `text-embedding-3-small`（1536 维）并校验最终向量维度；超时和最多 2 次重试仅由网关执行，业务适配器不得再次重试，于 `backend/app/services/llm/embeddings.py`
- [x] T056 [US1] 实现 `DocumentPipelineOrchestrator` 与 `finalize/cleanup`：统一事务锁定并校验 attempt/task/document/lease，完成当前阶段、幂等创建或激活下一阶段、更新 `current_task_type`/`lease.task_id`，并使 `documents.retry_count` 镜像当前任务、阶段切换重置为 0、finalize 完成时与阶段一起归零，提交后才投递；finalize 只经 `ChunkRepository` 校验并原子翻转 completed/chunk_count/释放 lease，不一致持久化 `20013`；cleanup 只清理旧版本，于 `backend/app/services/document_pipeline.py`、`backend/app/repositories/document_tasks.py`、`backend/app/workers/document_finalize.py`、`backend/app/workers/document_cleanup.py`
- [x] T057 [US1] 实现仅供资料 DELETE 使用、强制 `user_id` 且可锁定命中普通可见资料/`deleting`/`failed/delete_cleanup/20015` 的变更查询，禁止复用于 GET/list；首次删除置 `deleting`、取消未开始任务、递增 `delete_cycle`、新建 `retry_count=0` 的专用 `delete_cleanup` 并同步重置文档镜像计数，命中 `deleting` 时幂等成功且不递增轮次/不建任务，命中删除失败态时才递增轮次并以相同规则新建任务，`deleted` 返回 404；无活动 attempt 时释放 lease/激活清理，有活动 attempt 时锁定并以当时 `expires_at` 冻结上限且禁止心跳续租，无活动 lease 则立即接管；扫描器到期后取消 attempt/task、释放 lease并激活清理；清理原始对象与全部派生数据、置空引用外键但保留必填快照并保留 `deleted` 墓碑；重试耗尽转为 `failed/delete_cleanup/20015` 且仅返回最小墓碑和 `retry_delete`，旧任务/attempt/retry_count 不可修改，于 `backend/app/repositories/documents.py`、`backend/app/services/document_deletion_service.py`、`backend/app/api/v1/routes/documents.py`、`backend/app/workers/document_delete_cleanup.py`、`backend/app/workers/task_recovery.py`
- [x] T058 [P] [US1] 运行并修复四类解析、上传事务/幂等、处理并发、嵌入网关和流水线状态集成测试于 `backend/tests/unit/services/parsers/`、`backend/tests/integration/documents/test_upload_and_access.py`、`backend/tests/integration/documents/test_upload_idempotency.py`、`backend/tests/integration/documents/test_processing_concurrency.py`、`backend/tests/integration/documents/test_pipeline_state_machine.py`
- [x] T059 [P] [US1] 运行并修复 Celery 投递失败后扫描器幂等重投递、失联处理名额回收、过期上传幂等记录清理和重试耗尽稳定错误码测试于 `backend/tests/integration/documents/test_task_recovery.py`

**检查点**：资料处理由持久化任务记录驱动；失败资料只显示失败原因与删除操作；MVP 无重处理或替换接口。

---

## 阶段 4：用户故事 2 — 基于资料进行可信连续问答（后端）

**目标**：让用户在已授权知识库中进行带来源引用的连续问答；没有证据时明确拒答。

**独立测试**：对已完成资料提问可获得 SSE 回答和引用；不相关问题、无完成资料、其他用户资料、旧版本或未完成资料均不能成为回答证据。

### 先写测试

- [x] T060 [P] [US2] 编写会话 CRUD/分页/消息状态和统一 Citation DTO 契约测试：assistant 严格配对 `streaming/null`、`completed/stop|length`、`failed/error`、`cancelled/cancelled`；`live` 强制两个 UUID、`snapshot` 强制两个 ID 为 null、定位/内容、rank 顺序和页码分页；覆盖知识库完成编排清理后的级联删除及跨用户会话/消息/引用统一 `20007/404`，于 `backend/tests/contract/test_conversations_api.py`
- [x] T061 [P] [US2] 编写向量/关键词双路检索只能通过统一 `ChunkRepository` 且强制用户、知识库、版本、完成状态过滤；断言分别低于 `RETRIEVAL_VECTOR_MIN_SIMILARITY`/`RETRIEVAL_TRGM_MIN_SIMILARITY` 的候选在 RRF 前排除，以及 RRF 的失败测试于 `backend/tests/integration/retrieval/test_tenant_version_filters.py`、`backend/tests/unit/services/test_retrieval.py`
- [x] T062 [P] [US2] 编写无完成资料和两路门槛过滤后为空时的可信拒答测试，断言不调用生成模型、不创建 Citation 且为 `completed/stop`，于 `backend/tests/unit/services/test_answer_rejection.py`
- [x] T063 [P] [US2] 编写原始 SSE 文本帧、五类判别事件、`retrieval_done` 与详情复用 Citation 字段语义；断言正常/可信无证据为 `completed/stop`，供应商/模型/服务错误重试耗尽发送 `error` 且持久化 `failed/error`，客户端连接断开固定为 `cancelled/cancelled`，并覆盖 API 进程中断后维护扫描器只条件收敛超时 `streaming` 为 `failed/error`，于 `backend/tests/contract/test_messages_sse_api.py`、`backend/tests/integration/conversations/test_sse_terminal_states.py`
- [x] T064 [P] [US2] 编写改写、reranker 和生成网关配置、超时、重试、网关最终失败分类与业务领域降级、脱敏失败零外发的失败测试；Reranker 合法结果按 score 降序且同分保持 RRF 原顺序，缺项、重复/越界序号、非有限 score 或非法 JSON 必须整体回退 RRF；生成失败重试耗尽必须为 `failed/error`，不得误记为 cancelled，于 `backend/tests/unit/services/llm/test_resilience.py`

### 后端实现

- [x] T065 [US2] 实现必须绑定当前用户知识库的会话及消息仓储/服务于 `backend/app/services/conversation_service.py`、`backend/app/repositories/conversations.py`
- [x] T066 [US2] 实现会话 CRUD、可空标题/最后消息时间、会话/引用页码分页和消息游标分页路由/判别模式，强制 user 消息为 completed、assistant 消息为 streaming 或明确终态；知识库在资料清理完成后才级联对话/消息/引用，资料删除保留引用快照，于 `backend/app/api/v1/routes/conversations.py`、`backend/app/api/v1/schemas/conversations.py`
- [x] T067 [US2] 在统一 `ChunkRepository` 中实现向量召回并强制 `user_id`、知识库、当前版本、`completed` 与 documents join 过滤，以及 `RETRIEVAL_VECTOR_MIN_SIMILARITY` 的 SQL 门槛，于 `backend/app/repositories/chunks.py`
- [x] T068 [US2] 在同一 `ChunkRepository` 中实现 pg_trgm 关键词召回并复用相同租户/版本/完成状态过滤构造器，以及 `RETRIEVAL_TRGM_MIN_SIMILARITY` 的 SQL 门槛，于 `backend/app/repositories/chunks.py`
- [x] T069 [US2] 实现只消费通过门槛候选的 RRF、合法 reranker 评分按 score 降序且同分保持 RRF 原顺序、3000 token 上下文打包和相邻片段去重；融合为空时返回无证据结果，不调用 Reranker 或生成，于 `backend/app/services/retrieval_service.py`
- [x] T070 [US2] 实现只依赖内部 `ModelGateway` 的可选 reranker 用例适配器；10 秒超时和 1 次重试仅由网关执行，业务适配器消费最终结果，失败时整体返回原 RRF 顺序且不应用部分评分，于 `backend/app/services/llm/reranker.py`
- [x] T071 [US2] 实现只依赖内部 `ModelGateway` 的查询改写/生成用例适配器和最近三轮最小上下文；改写 10 秒/1 次、生成首 token 15 秒/总时长 120 秒/1 次的超时重试只由网关执行，业务层仅在最终改写失败后使用原问题、最终生成失败后收敛 `failed/error`；实现无证据可信答复 `completed/stop` 和 `20005/409 KNOWLEDGE_BASE_NOT_READY` 于 `backend/app/services/answer_service.py`、`backend/app/services/llm/chat.py`
- [x] T072 [US2] 实现统一 Citation DTO：当前来源返回 `source_type=live`，删除/不可访问来源将 ID 置空并从快照返回 `source_type=snapshot`、文件类型、定位和内容预览，按 rank 排序，于 `backend/app/services/citation_service.py`
- [x] T073 [US2] 按 OpenAPI 的文本线格式与 `x-sse-event-schema` 判别联合实现五类统一信封事件；正常结束写 `completed/stop|length`，服务错误发送 `error` 并写 `failed/error`，客户端连接断开写 `cancelled/cancelled`，于 `backend/app/api/v1/sse/message_stream.py`
- [x] T074 [US2] 将消息发送路由接入检索、生成、引用和 SSE 流，并用单一终态收敛器保证所有异常分支离开 `streaming`；扩展既有维护扫描器，对 `status=streaming AND created_at < now()-MESSAGE_STREAMING_STALE_SECONDS` 的 assistant 消息原子更新为 `failed/error`，不得覆盖已终态消息，于 `backend/app/api/v1/routes/messages.py`、`backend/app/services/message_terminal_state.py`、`backend/app/workers/task_recovery.py`
- [x] T075 [P] [US2] 运行并修复确定性的 RRF、有证据回答保存字段完整 Citation、无证据拒答和删除后 snapshot 功能测试于 `backend/tests/unit/services/test_retrieval.py`、`backend/tests/unit/services/test_answer_rejection.py`、`backend/tests/unit/services/test_citations.py`
- [x] T076 [P] [US2] 运行并修复 SSE 原始帧、解码判别事件、API 进程中断后的超时 streaming 扫描恢复，以及 `completed/stop|length`、`failed/error`、`cancelled/cancelled` 三类 assistant 终态契约/集成测试于 `backend/tests/contract/test_messages_sse_api.py`、`backend/tests/integration/conversations/test_sse_terminal_states.py`
- [x] T077 [P] [US2] 运行并修复改写、reranker 和生成全部经网关、网关最终失败与业务领域降级职责、配置选择、Reranker 评分完整性/稳定排序/非法响应整体回退、超时、重试、脱敏失败回退及生成失败最终 `failed/error` 测试于 `backend/tests/unit/services/llm/test_resilience.py`

**检查点**：所有回答仅以当前用户、知识库、已完成当前版本资料为证据；SSE 事件使用统一信封；纯聊天模式不存在。

---

## 阶段 5：用户故事 3 — 掌握资料处理与异常结果（后端）

**目标**：让用户诊断资料处理并安全删除资料，同时维持历史回答的来源快照。

**独立测试**：用户可查看资料和任务的明确终态与失败原因；删除资料后新问答排除其内容，历史回答仅显示不可恢复的快照。

### 先写测试

- [x] T078 [P] [US3] 编写资料/任务各自状态枚举、任务阶段、完整尝试记录 DTO（worker、非空 started_at、可空结束/错误/耗时）、持久化 `error_code`、失败原因仅对所有者可见和失败后无重处理操作的 API 契约测试；断言公开资料 DTO/过滤仅允许 `pending/queued/processing/completed/failed`，`status=deleting/deleted` 返回 `10003/400` 且不能暴露隐藏资料，于 `backend/tests/contract/test_document_status_api.py`
- [x] T079 [P] [US3] 编写资料首次 DELETE 后立即隐藏并递增轮次/建任务、`deleting` 状态所属用户重复 DELETE 幂等 200 且轮次和任务数不变、`failed/delete_cleanup/20015` 重试才递增轮次/新建任务并保留旧历史、`deleted` 后 DELETE/GET 404；覆盖无运行 attempt 立即接管、运行写入被 fencing 拒绝、等待不超过 lease.expires_at、超时扫描 cancelled/释放/激活清理、清理失败最小墓碑/`retry_delete`；覆盖知识库 `active→deleting→delete_failed/20015→deleting` 和最终物理删除，断言 delete_failed 墓碑仅所属用户可见且不含名称/描述/子资源、重复 deleting DELETE 不建任务、重试仅为失败子资料新建轮次；覆盖检索排除、必填引用快照及明确终态，于 `backend/tests/integration/documents/test_deletion_and_citations.py`、`backend/tests/integration/documents/test_terminal_states.py`、`backend/tests/integration/knowledge_bases/test_deletion_orchestration.py`

### 后端实现

- [x] T080 [US3] 完善资料与任务详情服务的阶段、尝试、`20001/20010～20015/50000` 错误分类、失败原因和安全错误摘要映射；将 `failed/delete_cleanup/20015` 映射为删除未完成墓碑，而非普通处理失败，于 `backend/app/services/document_status_service.py`
- [x] T081 [US3] 基于 T057 的资料删除编排实现非空知识库删除：知识库列表/详情以所属用户为范围返回 `active` 完整对象或 `delete_failed` 最小墓碑，内容与子资源读取只允许 `active`；另提供仅 DELETE 使用且强制 `user_id` 的锁定变更查询命中 `active/deleting/delete_failed`；将知识库置 `deleting` 并复用有界 lease、孤儿接管、delete_cleanup、名额释放与引用快照；命中 `deleting` 幂等且不建任务，子资料清理耗尽时维护扫描器置 `delete_failed/20015`，再次 DELETE 才转回 deleting 并仅为失败子资料新建轮次；全部子资料 `deleted` 且无活动 attempt 后物理删除，之后 DELETE 返回 404，于 `backend/app/repositories/knowledge_bases.py`、`backend/app/services/knowledge_base_service.py`、`backend/app/services/citation_service.py`、`backend/app/workers/task_recovery.py`
- [x] T082 [US3] 在资料/任务详情响应中暴露终态、契约限定的持久化失败码、失败原因和唯一允许的删除操作标识；普通失败为 `delete`，`failed/delete_cleanup/20015` 仅为 `retry_delete`，于 `backend/app/api/v1/schemas/documents.py`
- [x] T083 [P] [US3] 运行并修复“宁可明确失败、不展示无限处理中或 deleting”、删除 fencing、有界超时接管、删除失败墓碑/重试历史不可变与知识库清理编排集成测试于 `backend/tests/integration/documents/test_terminal_states.py`、`backend/tests/integration/documents/test_deletion_and_citations.py`、`backend/tests/integration/knowledge_bases/test_deletion_orchestration.py`

**检查点**：用户不会看到无结束处理状态；失败资料没有重处理/替换入口；历史引用保留快照但不暴露已删除原文。

---

## 阶段 6：A5 完成后的后端契约冻结门禁

**目的**：冻结后端业务规则与 `/v1` REST/SSE 契约。此阶段不开发前端功能。

- [x] T084 使实现与 OpenAPI 契约中的 Access Token 全部验证失败统一 `10001/401`、不透明 Refresh Token 并发单一轮换、注册/登录/限流复用邮箱规范化、刷新 `refresh-ip-and-token` 限流策略、跨租户知识库 `20002/404`/其他资源 `20007/404`、202 上传结果、公开资料状态与当前任务重试镜像、任务/attempt 重试预算和完整 Attempt DTO 语义、资料删除重放/失败墓碑、知识库 `delete_failed/20015` 最小墓碑与 retry_delete、Citation 条件、异步错误、上传接管/幂等、assistant 三类终态及失联 streaming 扫描恢复、可信代理限流和 SSE 判别事件一致于 `specs/001-orionamesh-rag-mvp/contracts/openapi.yaml`、`backend/app/api/v1/`（核查通过；修复非 404 HTTPException 业务码映射为 `10003`，见 `backend/app/api/middleware/errors.py`）
- [x] T085 [P] 运行并修复迁移、pgvector/pg_trgm、本地持久卷可写、处理名额、检索相似度阈值与消息 streaming 失联上限配置就绪检查于 `backend/tests/integration/test_startup_readiness.py`（26 passed/1 skipped；修复 `tests/conftest.py` 测试库自动创建的 AUTOCOMMIT 事务问题与 `alembic.ini` path_separator 告警）
- [x] T086 [P] 运行并修复 Access Token 全部验证失败的 `10001/401`、`10004/401` 仅登录凭证错误、`10006/401`、`20009/400`、异步 `20001/20010～20015` 固定安全提示、`10005/429`、`50001/503` 及所有非 SSE 操作的 `50000/500` 统一信封与 trace_id 契约测试于 `backend/tests/contract/test_business_error_codes.py`、`backend/tests/contract/test_rate_limits.py`（65 passed；修复限流错误响应重复 `X-Trace-Id` 头与单独运行时 schema 缺失）
- [x] T087 [P] 运行并修复认证会话撤销、邮箱规范化复用、可信代理来源 IP、租户隔离、公开读取不可见删除态、资料 deleting 重复 DELETE 无副作用/删除失败新轮次/deleted 后 404、未就绪 pending 不执行/300 秒上传接管/幂等、解析安全、阶段事务编排、attempt fencing、资料级处理名额、有界资料/知识库删除、删除中知识库重复 DELETE、删除失败墓碑、embed 直写/finalize 发布、检索过滤及相似度门槛拒答、非空引用快照与 assistant 三类终态及失联恢复全链路测试于 `backend/tests/integration/test_backend_gate.py`（30 passed）
- [x] T088 [P] 编写架构依赖测试：禁止供应商旁路；禁止路由/服务/worker 直接读取 chunks 或绕过任务尝试仓储；要求 attempt 创建锁定父任务并复制/校验租户边界且读取固定带用户范围；要求持久化写仓储复用 fencing guard；禁止按 ID 查询做全局存在性探测；禁止资料 DELETE 专用查询及知识库 `active/deleting/delete_failed` DELETE 查询被普通 GET/list/子资源调用，仅允许独立最小墓碑读取命中 delete_failed；禁止 delete_cleanup 与旧版本 cleanup 混用；验证层次依赖、Redis/Celery 不作为真相源及业务层不拼绝对路径，于 `backend/tests/architecture/test_model_gateway_boundaries.py`、`backend/tests/architecture/test_chunk_repository_boundaries.py`、`backend/tests/architecture/test_pipeline_fencing_boundaries.py`、`backend/tests/architecture/test_task_attempt_repository_boundaries.py`、`backend/tests/architecture/test_document_visibility_boundaries.py`、`backend/tests/architecture/test_knowledge_base_visibility_boundaries.py`、`backend/tests/architecture/test_layer_boundaries.py`、`backend/tests/architecture/test_task_truth_source.py`、`backend/tests/architecture/test_storage_boundaries.py`（68 passed）
- [x] T089 [P] 使用受控假供应商验证四类调用全部经网关、脱敏失败零网络请求、凭证边界、网关超时重试与最终失败分类、业务领域降级和日志白名单于 `backend/tests/integration/infrastructure/test_model_egress.py`（23 passed；修复 reranker 缺失 `candidate_count` 选项与 generation 网络超时为 0 的实现 bug）
- [x] T090 [P] 校验内部模型出口契约与实现的四类调用、最小字段、脱敏状态、必填 endpoint、`openai-compatible` provider 前置条件、网关唯一超时/重试执行与单次供应商适配请求、网关最终失败和业务领域降级职责、Reranker `scores[{candidate_index,score}]` 完整性/整体回退、未知 provider 拒绝和日志白名单一致于 `specs/001-orionamesh-rag-mvp/contracts/model-egress.md`、`backend/tests/contract/test_model_egress_contract.py`（68 passed）
- [x] T091 冻结后端 OpenAPI/SSE/内部模型出口契约并记录限流、出口安全与“A5 实现完成后的契约门禁”通过结果于 `specs/001-orionamesh-rag-mvp/contracts/openapi.yaml`、`specs/001-orionamesh-rag-mvp/contracts/model-egress.md`、`specs/001-orionamesh-rag-mvp/quickstart.md`（A5 门禁通过：全套件 713 passed/1 skipped，Ruff/Pyright 全绿，契约已冻结）

**检查点**：A5 实现及其契约冻结门禁通过后，后端是唯一可信业务实现；全部外部模型调用无旁路、脱敏失败无外发、日志无 payload，限流错误和故障语义已冻结；仍需完成 A6 工程化门禁后才允许开始前端 UI 或联调任务。

---

## 阶段 7：工程化、部署与 A6 交付门禁（前端开始前必须完成）

**目的**：把所有技术栈固定为可复现、可校验和可部署的工程基线。该阶段不得开发前端 UI。

- [x] T092 创建 pnpm 根工作区、Node 版本、共享脚本及唯一根锁文件策略于 `package.json`、`pnpm-workspace.yaml`、`pnpm-lock.yaml`、`.nvmrc`、`frontend/package.json`
- [x] T093 配置前端 pnpm 项目、Next.js/React/TypeScript/Tailwind/Shadcn/UI/Pino 依赖及开发命令于 `frontend/package.json`、`frontend/next.config.ts`、`frontend/src/app/layout.tsx`
- [x] T094 [P] 配置 ESLint、Prettier、TypeScript 严格检查和 Vitest 于 `frontend/eslint.config.mjs`、`frontend/.prettierrc.json`、`frontend/tsconfig.json`、`frontend/vitest.config.ts`、`frontend/tests/setup.ts`
- [x] T095 [P] 配置 Pino 仅用于 Next.js 服务端日志，并过滤 token、密码、资料内容和引用快照于 `frontend/src/lib/logging/server.ts`
- [x] T096 [P] 按 Quickstart 固定变量名创建认证、限流、持久卷/解析/并发、processing lease、upload pending 超时、幂等及模型网关环境模板；认证只包含必填且至少 32 字节的 `AUTH_JWT_SECRET_KEY`，不得提供算法/TTL 覆盖变量；模型包含必填 `MODEL_GATEWAY_ENDPOINT`、`MODEL_GATEWAY_PROVIDER=openai-compatible`，Query Rewrite/Generation 必填、Embedding 默认、Reranker 空值禁用，endpoint 示例使用 HTTPS（本机回环开发例外可用 HTTP），非法认证配置、缺失/不允许的 HTTP endpoint 或未知 provider 拒绝就绪；模板按 quickstart 环境契约提供 `backend/.env.local.example`（本地开发）与 `backend/.env.test.example`（自动化测试），部署环境不提供仓库内模板、由 Docker/CI 注入，于 `.env.example`、`backend/.env.local.example`、`backend/.env.test.example`、`frontend/.env.example`
- [x] T097 创建后端多阶段 Docker 镜像并用 uv 锁定安装 PyMuPDF、python-docx、markdown-it-py、charset-normalizer 等依赖于 `deploy/docker/backend.Dockerfile`
- [x] T098 [P] 创建前端多阶段 Docker 镜像并用根 pnpm 锁文件锁定安装与构建于 `deploy/docker/frontend.Dockerfile`
- [x] T099 创建 PostgreSQL、Redis、单次串行 `alembic upgrade head` 的 one-off migrate、后端 API、Celery worker 和前端的 Docker Compose 编排，为 API/worker 共同挂载 `/data/orionamesh` 命名持久卷并验证共用网关/限流配置；T133 修订为 API/worker 使用同一必填 `BACKEND_IMAGE`、前端使用必填 `FRONTEND_IMAGE`，服务器不再回退到 build，于 `deploy/compose/compose.yaml`
- [x] T100 [P] 创建后端质量、迁移（含 last_login 可空、Citation 非空/唯一、delete_cleanup、知识库 `active/deleting/delete_failed` 与 `delete_error_code=20015` 配对约束）、扩展、OpenAPI、SSE、解析依赖、本地卷、HS256 认证配置、限流和模型网关必填 endpoint/评分契约校验脚本于 `scripts/check-backend.sh`、`scripts/verify-contracts.sh`
- [x] T101 [P] 创建前端 pnpm lint、format、类型检查、单测和端到端校验脚本于 `scripts/check-frontend.sh`
- [x] T102 创建 GitHub Actions PR CI：uv/根 pnpm 锁定安装、Ruff、Pyright、ESLint、Prettier、类型检查、单元/集成/契约/架构测试、迁移与契约校验于 `.github/workflows/ci.yml`
- [x] T103 创建 GitHub Actions 双镜像构建与 Trivy 漏洞扫描门禁工作流（HIGH/CRITICAL 即失败）；正式 tag 的镜像归档与 GitHub Release 交付由 T133 修订，于 `.github/workflows/image.yml`
- [x] T104 [P] 编写 Docker Compose 健康/就绪、HS256 密钥缺失/过短、模型 endpoint 缺失/非法/非回环 HTTP 时拒绝就绪且 HTTPS 与本机回环 HTTP 可用、one-off 迁移成功后才切换 API/worker、迁移失败保持旧容器、API/worker 不自动迁移、容器重建后持久卷保留、锁文件不可变安装、GitHub Release 打包与仅 Nginx 公开端口静态契约测试于 `backend/tests/integration/test_delivery_stack.py`
- [x] T105 运行 A6 工具链、Compose、持久卷重建、阶段编排/fencing/删除接管、CI workflow、可信代理限流/出口安全门禁与双镜像构建验证；GitHub Release 实际产物与服务器首次部署验收由未完成的 T133a/T133b 独立记录，保留“串行 one-off Alembic 成功后才切换容器、API/worker 不自动迁移、镜像回滚不自动降级数据库、破坏性迁移先人工备份”原则。

**检查点**：uv、pnpm、全部质量工具、Docker/Compose、本地持久卷、CI/CD、可配置模型网关、Redis 限流与出口安全已验证；T105 后才允许前端 UI。

---

## 阶段 8：前端基础与用户故事 1 渲染（仅在 T105 后）

**目标**：基于冻结契约实现认证、知识库和资料上传/状态展示，不在前端复制后端规则。

**独立测试**：用户可在浏览器中登录、创建知识库、上传受限资料、轮询最终状态并删除失败资料。

### 先写测试

- [ ] T106 [P] [US1] 编写 API 客户端按 `code` 处理同步错误和详情 HTTP `200` 内异步 `error_code`、上传 `Idempotency-Key`、分页、trace_id 与限流的失败测试于 `frontend/tests/unit/lib/api/client.test.ts`
- [ ] T107 [P] [US1] 编写认证、查看/更新本人基本资料、知识库列表与 `delete_failed/20015` 墓碑/重试删除、上传限制/重放、轮询终态、空文档失败删除、`allowed_actions` 和 404 不可见资源提示组件失败测试于 `frontend/tests/component/user-story-1.test.tsx`

### 前端实现

- [ ] T108 [US1] 实现 API 信封、Bearer 会话及携带 refresh token 请求体的登出、trace_id、SSE、上传幂等键/协调中 409、分页、同步业务码和异步资源 `error_code` 客户端封装于 `frontend/src/lib/api/client.ts`、`frontend/src/lib/api/types.ts`
- [ ] T109 [US1] 实现登录、注册、会话恢复、受保护路由以及查看/更新本人基本资料于 `frontend/src/app/(auth)/login/page.tsx`、`frontend/src/app/(auth)/register/page.tsx`、`frontend/src/app/profile/page.tsx`、`frontend/src/features/auth/`、`frontend/src/features/profile/ProfileForm.tsx`
- [ ] T110 [US1] 实现知识库页码列表、创建、编辑和删除渲染；`delete_failed/20015` 仅显示最小“删除未完成”墓碑与 `retry_delete`，不得显示名称、描述或子资源入口，于 `frontend/src/app/knowledge-bases/page.tsx`、`frontend/src/features/knowledge-bases/`
- [ ] T111 [US1] 实现资料批量选择/拖放、50MB/20 文件提示、请求级幂等键和上传进度渲染于 `frontend/src/features/documents/UploadPanel.tsx`
- [ ] T112 [US1] 实现资料页码/状态列表、完整 DTO 详情轮询、`20001/20010～20015/50000` 固定安全提示及 `allowed_actions` 操作渲染；`failed/delete_cleanup/20015` 只能显示最小“删除未完成”墓碑与重试删除，不得作为普通失败资料展示，于 `frontend/src/features/documents/DocumentList.tsx`、`frontend/src/features/documents/DocumentDetail.tsx`

**检查点**：前端只消费阶段 6 冻结的后端接口；资料失败不显示重处理/替换；所有错误以 `code/msg/trace_id` 表现。

---

## 阶段 9：前端用户故事 2 渲染（仅在 T105 后）

**目标**：渲染必须绑定知识库的会话、历史消息、SSE 回答、拒答提示和来源引用。

**独立测试**：用户可选择知识库创建对话、看到流式回答和引用；无证据与取消均有明确反馈。

### 先写测试

- [ ] T113 [P] [US2] 编写知识库绑定、可空会话标题、分页、五类 SSE 增量、无证据、`failed/error`、`cancelled/cancelled` 和引用快照组件失败测试于 `frontend/tests/component/user-story-2.test.tsx`

### 前端实现

- [ ] T114 [US2] 实现知识库绑定会话页码列表、可空标题展示、创建、重命名与删除界面于 `frontend/src/app/conversations/page.tsx`、`frontend/src/features/conversations/ConversationList.tsx`
- [ ] T115 [US2] 实现消息历史分页、提问和五类判别式 SSE 信封事件解析，分别保留正常、服务失败和客户端取消终态于 `frontend/src/features/conversations/MessageThread.tsx`、`frontend/src/features/conversations/useMessageStream.ts`
- [ ] T116 [US2] 实现无完成资料/无证据、`failed/error`、`cancelled/cancelled`、Token 过期、当前租户不可见资源 404、限流与 trace_id 用户提示于 `frontend/src/features/conversations/ConversationFeedback.tsx`
- [ ] T117 [US2] 按统一 Citation DTO 实现引用页码按需加载、rank 顺序、`source_type=live` 当前来源与 ID 为空的 `snapshot` 快照抽屉，于 `frontend/src/features/citations/CitationDrawer.tsx`

**检查点**：前端不允许创建纯聊天；SSE 流与失败都按统一信封解析；引用快照不可恢复原始资料。

---

## 阶段 10：前端用户故事 3 渲染与端到端联调（仅在 T105 后）

**目标**：完成处理诊断、删除体验和浏览器端的端到端质量门禁。

**独立测试**：资料处理失败或中断时用户可理解终态和失败原因；删除后新问答排除资料，历史引用仍显示快照。

### 先写测试与替身

- [ ] T118 [P] [US3] 编写完整 DocumentTask DTO、异步失败码、失败终态、删除确认、`20015` 删除未完成墓碑/重试删除与不可见重处理入口组件失败测试于 `frontend/tests/component/user-story-3.test.tsx`
- [ ] T119 [US3] 配置前后端契约替身与浏览器端到端测试环境于 `frontend/tests/e2e/fixtures/api.ts`、`frontend/playwright.config.ts`
- [ ] T120 [US3] 编写认证、本人基本资料查看/更新、上传重放、限流、轮询、空文档失败删除、资料与知识库删除中重复 DELETE 无副作用、`delete_failed/20015` 墓碑与重试删除、对话、SSE 三类终态、引用快照及跨用户知识库 `20002/404`/其他资源 `20007/404` 端到端失败测试于 `frontend/tests/e2e/orionamesh-mvp.spec.ts`

### 前端实现

- [ ] T121 [US3] 实现完整 DocumentTask DTO 的尝试、进度、处理阶段、持久化失败码、失败原因、删除确认和 `20015` 删除未完成的重试删除渲染于 `frontend/src/features/documents/TaskHistory.tsx`、`frontend/src/features/documents/DeleteDocumentDialog.tsx`

**检查点**：浏览器端完整主路径通过；前端未绕过后端授权、状态机或契约。

---

## 阶段 11：文档与跨切面收尾

- [ ] T122 [P] 更新开发环境、uv/根 pnpm 锁文件、持久卷、解析器、处理并发、上传幂等、可信代理限流、模型网关、GitHub Release 镜像归档部署与回滚（`scripts/deploy.sh`，服务器不构建、不访问 GHCR）说明于 `README.md`
- [ ] T123 [P] 执行并记录快速验证清单的最终结果于 `specs/001-orionamesh-rag-mvp/quickstart.md`
- [ ] T124 审查普通日志、模型调用审计、响应、SSE 和引用快照，确认不含 password/token/secret_key、请求/响应 payload、提示词、问题、片段、文件名、请求头或已删除原始资料于 `backend/app/core/logging.py`、`backend/app/infrastructure/model_gateway/audit.py`、`frontend/src/lib/logging/server.ts`、`backend/tests/integration/test_backend_gate.py`
- [ ] T125 运行全部确定性后端与前端测试、迁移、OpenAPI/模型出口契约、上传超时接管、阶段编排、写入 fencing、有界资料/知识库删除、删除失败墓碑与下一轮清理历史、解析安全、处理并发、持久卷、架构边界、限流/出口安全、质量工具与 Compose 验证，并记录结果于 `specs/001-orionamesh-rag-mvp/quickstart.md`

## 评审修复（T126–T131）

以下条目为阶段 7 后代码评审发现并修复的资源边界/失败收敛缺陷；代码与测试随条目交付。

- [x] T126 [P] 修复流式生成资源边界：SSE 客户端断连只收敛数据库终态、不停止后台生成线程与模型流；生成链（SSE 生产者 → 网关 `call_stream` → `_stream_rest`/`_first_chunk` 生产者）以停止事件贯穿，消费方退出（断连/超时/生成器关闭）时生产者停止拉取并 `close()` 供应商生成器中止物理请求，不再阻塞在满队列上持续消耗供应商连接与配额；SSE 断连测试断言生成流被关闭且停止拉取、网关超时测试断言供应商流被关闭，于 `backend/app/api/v1/sse/message_stream.py`、`backend/app/infrastructure/model_gateway/service.py`、`backend/tests/integration/conversations/test_sse_terminal_states.py`、`backend/tests/unit/infrastructure/model_gateway/test_gateway.py`
- [x] T127 [P] 修复解析超时资源边界：解析改在 spawn 子进程中执行，超时终止子进程硬性回收 CPU/内存（daemon 线程无法被强制终止）；解析器以模块级类引用跨进程重建；`ParseError` 显式 `__reduce__` 保证跨进程重建（`Exception.__reduce__` 用 `self.args` 会丢 code）；父进程先取结果再回收进程，避免大结果写入管道缓冲死锁，于 `backend/app/services/parsers/security.py`、`backend/app/services/parsers/base.py`、`backend/tests/unit/services/parsers/test_document_parsers.py`
- [x] T128 [P] 修复解析对象写入失败未收敛：`write_object` 失败立即以 `20011` 文件持久化失败收敛 attempt/task/document（修复前异常逃逸，attempt 停留 running 等待租约过期），于 `backend/app/workers/document_parse.py`、`backend/tests/integration/documents/test_pipeline_state_machine.py`
- [x] T129 [P] 修复解析对象泄漏：数据库保存/阶段提交失败（非 fencing）时清理已写解析对象，不遗留 `delete_cleanup` 无法发现的无主派生对象（fencing 分支原有清理不变），于 `backend/app/workers/document_parse.py`、`backend/tests/integration/documents/test_pipeline_state_machine.py`
- [x] T130 [P] 修复限流中间件忽略注入 `Settings`：`_user_fingerprint` 改用 `self.settings.auth_jwt_secret_key_value` 验签（修复前用全局 `get_settings()`，自定义应用/测试配置下合法用户 token 解码失败、静默跳过用户级限流），于 `backend/app/api/middleware/rate_limit.py`、`backend/tests/unit/infrastructure/rate_limit/test_middleware.py`
- [x] T131 [P] 修复限流中间件回放截断请求体：请求体超过 64KB 小 JSON 上限时明确拒绝 `413/10003`，不再把截断内容回注给下游（修复前下游收到被篡改的截断 body），于 `backend/app/api/middleware/rate_limit.py`、`backend/tests/unit/infrastructure/rate_limit/test_middleware.py`

## 部署方式变更（T132–T133）

- [x] T132 [P] 历史方案：腾讯云服务器本地构建，已被 T133 取代。
- [x] T133 [P] 变更为 GitHub Release 镜像归档交付：`image.yml` 在 PR 保留 `linux/amd64`
  双镜像构建与 Trivy HIGH/CRITICAL 门禁（main 为受保护分支，仅 PR 合并，合并后不重复构建），在正式 `v*` tag 导出 backend/frontend 镜像 tar、镜像引用
  清单、Compose、Nginx 和部署脚本，生成 SHA-256 并发布公开 GitHub Release；Nginx bind mount 必须由
  部署脚本注入已安装配置的绝对宿主机路径，不得依赖相对路径；Compose 强制完整
  `BACKEND_IMAGE`/`FRONTEND_IMAGE`、不再含服务器 `build`、仅 Nginx 发布 80、PostgreSQL/Redis/API/
  worker/前端均不暴露主机端口；`scripts/deploy.sh` 校验后的发布包通过 `docker image load` 导入应用镜像，
  先执行 one-off migrate，成功后才以 `--no-build --pull never` 更新服务；回滚导入上一 Release，不自动降级
  数据库。同步 `quickstart.md`、`README.md`、`docs/OrionaMesh.md` 与交付栈测试。
- [x] T133a [T133] 推送临时正式 `v*` tag，确认 GitHub Actions 成功创建公开 Release；下载资产并验证外层
  `.sha256`、包内 `release.files.sha256`、两份镜像 tar、`release.env` 和运行时配置完整，记录 run URL 与
  SHA-256 于 `quickstart.md`。
- [ ] T133b [T133] 在腾讯云 Ubuntu x86_64 首次部署（v0.1.1 已通过，见 quickstart「T133 交付验证记录」；
  升级与回滚待第二 tag）：以无业务数据的旧 Compose 栈释放 80 端口，创建
  `0600` 的 `/opt/orionamesh/.env`，运行发布包内 `scripts/deploy.sh`；验证 Nginx 挂载的是
  `/opt/orionamesh/deploy/nginx/nginx.conf` 而非兼容路径、仅 80 监听、`docker compose ps`
  全部就绪、PostgreSQL 的 `vector/pg_trgm/pgcrypto` 扩展、Redis 认证、`/` 反向代理和 Compose 内 API `/ready`，
  并用 `docker network inspect` 核对 Compose 网络实际子网落在 `RATE_LIMIT_TRUSTED_PROXY_CIDRS` 内
  （不匹配时用实际子网更新 `.env` 后重新部署）。
  再以第二个 tag 升级并以首个 tag 回滚；确认两次均不发生应用镜像构建、不会自动数据库降级且资料持久卷保留。

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
