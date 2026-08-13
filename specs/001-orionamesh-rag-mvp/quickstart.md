# 快速验证：OrionaMesh 个人知识库 RAG MVP

## 前置条件

- 可用的 PostgreSQL，已启用向量与相似文本匹配扩展。
- 可用的 Redis 与后台工作进程；MVP 将命名持久卷挂载到 `/data/orionamesh`，容器重建后资料必须保留。
- 配置模型供应商、模型、端点和凭证；所有外部模型调用必须经过后端内部模型出口网关，
  业务服务与 worker 不得直连供应商。
- 配置限流键摘要秘密和四类阈值；不得在 Redis 键中保存明文邮箱、用户标识、令牌或请求内容。
- 日志输出不得记录凭证、原始令牌、提示词、用户问题、资料片段、文件名、请求/响应正文或请求头。
- 使用 `uv sync --locked` 安装后端依赖，使用 `pnpm install --frozen-lockfile` 安装前端依赖；
  仓库只允许根目录 `pnpm-lock.yaml`，锁文件缺失或过期时验证必须失败。

### 资料处理配置契约

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `DOCUMENT_STORAGE_ROOT` | `/data/orionamesh` | 本地持久卷根目录；数据库只保存相对对象键 |
| `DOCUMENT_PROCESSING_MAX_PER_USER` | `3` | 单用户同时持有的资料处理名额 |
| `DOCUMENT_PROCESSING_LEASE_SECONDS` | `300` | worker 心跳失联后的名额回收边界 |
| `DOCUMENT_UPLOAD_PENDING_TIMEOUT_SECONDS` | `300` | 上传文件转正协调失联后的批次接管边界 |
| `DOCUMENT_PARSE_TIMEOUT_SECONDS` | `60` | 单份资料解析超时 |
| `DOCUMENT_PARSE_MAX_EXPANDED_BYTES` | `209715200` | DOCX 等归档格式解压后总大小上限（200MB） |
| `DOCUMENT_UPLOAD_IDEMPOTENCY_TTL_SECONDS` | `86400` | 上传 `Idempotency-Key` 结果保留期（24 小时） |

PDF、DOCX、MD、TXT 分别由 PyMuPDF、python-docx、markdown-it-py、charset-normalizer 处理；依赖版本
由 `uv.lock` 固定。解析过程不得发起外部请求、执行宏/脚本或读取对象键之外的本地路径。

### 限流配置契约

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `RATE_LIMIT_SUBJECT_HMAC_KEY` | 无 | 必填；对账号、用户和租户标识生成不可逆限流键摘要 |
| `RATE_LIMIT_TRUSTED_PROXY_CIDRS` | 空 | 可信反向代理 CIDR 逗号列表；为空时忽略所有转发头并使用直连对端 IP |
| `RATE_LIMIT_AUTH_IP_LIMIT` / `RATE_LIMIT_AUTH_IP_WINDOW_SECONDS` | `20` / `300` | 认证来源 IP 限制 |
| `RATE_LIMIT_AUTH_ACCOUNT_LIMIT` / `RATE_LIMIT_AUTH_ACCOUNT_WINDOW_SECONDS` | `5` / `300` | 注册/登录使用规范化邮箱 HMAC 摘要；刷新使用 refresh token HMAC 指纹 |
| `RATE_LIMIT_UPLOAD_LIMIT` / `RATE_LIMIT_UPLOAD_WINDOW_SECONDS` | `10` / `600` | 每用户上传限制 |
| `RATE_LIMIT_QUESTION_LIMIT` / `RATE_LIMIT_QUESTION_WINDOW_SECONDS` | `20` / `60` | 每用户问答限制 |
| `RATE_LIMIT_DEFAULT_LIMIT` / `RATE_LIMIT_DEFAULT_WINDOW_SECONDS` | `120` / `60` | 其他已认证接口限制 |
| `RATE_LIMIT_READ_FAIL_OPEN` | `true` | Redis 不可用时只读 GET 是否降级放行；状态变更始终 fail-closed |

`RATE_LIMIT_SUBJECT_HMAC_KEY` 不得与 JWT、供应商或数据库凭证复用；缺失时应用不得报告就绪。
`RATE_LIMIT_TRUSTED_PROXY_CIDRS` 中任一 CIDR 非法时应用不得报告就绪；请求携带的非法转发链只
回退到直连对端，不得导致限流旁路。

### 认证配置契约

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `AUTH_JWT_SECRET_KEY` | 无 | 必填；UTF-8 编码后至少 32 字节，仅用于 HS256 Access Token 签名 |

算法 `HS256` 与有效期 7200 秒是代码常量，不提供环境变量覆盖。Access Token 必须包含 `sub`、
`iat`、`exp` 和 `type=access`。验证端只允许 `HS256`，不得根据 token 头动态选择算法。
`AUTH_JWT_SECRET_KEY` 不得与限流、供应商或数据库凭证复用。
Access Token 缺失、Bearer 格式错误、签名或算法无效、必填声明错误、`type` 错误或过期均返回
`10001/401` 与“请重新登录”；`10004/401` 仅用于登录邮箱或密码不匹配。

### 模型出口配置契约

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `MODEL_GATEWAY_PROVIDER` | `openai-compatible` | 必填；MVP 唯一承诺的适配器标识，未知值不得启动 |
| `MODEL_GATEWAY_ENDPOINT` | 无 | 必填；必须为 HTTPS base URL；仅本地开发/自动化测试允许主机名精确为 `localhost` 或回环 IP `127.0.0.1`/`::1` 的 HTTP 地址 |
| `MODEL_GATEWAY_API_KEY` | 无 | 必填；只在发送边界读取和注入 |
| `MODEL_GATEWAY_SANITIZER_POLICY_VERSION` | `v1` | 脱敏规则版本；未知版本不得启动 |
| `MODEL_GATEWAY_AUDIT_PAYLOADS` | `false` | 必须保持 `false`；启动校验拒绝开启正文日志 |
| `MODEL_GATEWAY_EMBEDDING_MODEL` | `text-embedding-3-small` | 必填；默认 1536 维，变更维度必须迁移并重建向量 |
| `MODEL_GATEWAY_QUERY_REWRITE_MODEL` | 无 | 必填；查询改写模型 |
| `MODEL_GATEWAY_RERANK_MODEL` | 空 | 可选；为空时禁用 reranker 并直接使用 RRF |
| `MODEL_GATEWAY_GENERATION_MODEL` | 无 | 必填；回答生成模型 |
| `MODEL_GATEWAY_EMBEDDING_TIMEOUT_SECONDS` / `MODEL_GATEWAY_EMBEDDING_MAX_RETRIES` | `30` / `2` | 嵌入超时与最大重试次数 |
| `MODEL_GATEWAY_QUERY_REWRITE_TIMEOUT_SECONDS` / `MODEL_GATEWAY_QUERY_REWRITE_MAX_RETRIES` | `10` / `1` | 改写失败后使用原问题 |
| `MODEL_GATEWAY_RERANK_TIMEOUT_SECONDS` / `MODEL_GATEWAY_RERANK_MAX_RETRIES` | `10` / `1` | 重排失败后直接使用 RRF |
| `MODEL_GATEWAY_GENERATION_FIRST_TOKEN_TIMEOUT_SECONDS` | `15` | 生成首 token 超时 |
| `MODEL_GATEWAY_GENERATION_TOTAL_TIMEOUT_SECONDS` / `MODEL_GATEWAY_GENERATION_MAX_RETRIES` | `120` / `1` | 生成总时长与最大重试次数 |

所有适配器共用 `MODEL_GATEWAY_API_KEY`，不得创建独立供应商密钥变量。除上述本机回环例外外，HTTP
endpoint 必须拒绝就绪且无需环境模式变量。endpoint 缺失或非法、除可选 Reranker 外的必填模型缺失、
超时小于 1 秒或重试次数小于 0 时应用不得报告就绪。

Reranker 启用时，chat 端点响应必须是
`{"scores":[{"candidate_index":0,"score":0.0}]}`。每个输入候选序号必须恰好出现一次，序号不得
重复或越界，score 必须为有限数值；任何解析或校验失败均整体回退原 RRF 顺序。合法结果按 score
降序排序，同分保持 RRF 原顺序。

### 检索与消息恢复配置契约

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `RETRIEVAL_VECTOR_MIN_SIMILARITY` | `0.65` | 余弦相似度门槛；低于该值的向量候选不得进入 RRF |
| `RETRIEVAL_TRGM_MIN_SIMILARITY` | `0.30` | pg_trgm 相似度门槛；低于该值的关键词候选不得进入 RRF |
| `MESSAGE_STREAMING_STALE_SECONDS` | `360` | API 进程失联后的 assistant `streaming` 最大存续时间；维护扫描器超过该时间条件更新为 `failed/error` |

两个检索阈值必须在闭区间 `[0, 1]`。`MESSAGE_STREAMING_STALE_SECONDS` 必须不小于全部 Query Rewrite、
Reranker 和 Generation 最大尝试预算之和再加 60 秒；默认值 360 秒满足默认模型配置。配置不合法时应用不得报告就绪。
两路过滤及融合后无候选时，问答服务直接返回可信无证据答复，不调用回答生成模型。

## 后端优先验证

1. 运行迁移与启动检查；确认必需扩展可用，缺失时应用不得报告就绪。
2. 调用成功与失败接口；验证非 SSE 响应都包含 `code`、`data`、`msg` 和 UUID `trace_id`，成功
   `code=0`，前端可按非零 `code` 判断业务错误。
3. 注册两个用户，分别登录并刷新一次登录状态；并发使用同一 Refresh Token 发起两次刷新，验证数据库锁定使
   恰好一个请求成功并创建一个后继会话，另一个返回 `10006/401`。验证 Access Token 只接受 HS256、包含
   `sub/iat/exp/type=access` 且有效期为 2 小时；验证缺失、Bearer 格式错误、签名/算法无效、必填声明或
   `type` 错误和过期均返回 `10001/401`，而登录邮箱或密码错误才返回 `10004/401`；缺失/过短密钥时应用不得报告就绪，并验证算法和
   TTL 不存在环境变量覆盖入口。Refresh Token
   是 `rt_` + 32 字节 CSPRNG 的无填充 Base64URL 编码（总长 46）、有效期 7 天且不是 JWT，明文仅返回一次、数据库只存 SHA-256 摘要，旧刷新会话被撤销，且 password/token/secret_key 不出现在结构化日志中。
4. 用户 A 创建知识库。用户 B 读取、更新或删除该知识库时应得到 `20002/404`，访问其资料、任务、
   任务尝试、会话及引用时应得到 `20007/404`；服务端不得通过全局存在性探测泄露资源是否属于用户 A。
5. 用户 A 一次上传多份有效资料，验证 `202` 中每项只能为 `queued` 或 `failed/20011`；轮询资料详情直到每份为 `completed` 或 `failed`。模拟数据库事务失败应得到 `50000/500` 且无对象/记录；模拟任一对象转正失败应得到 `202`，整批资料/任务/幂等响应快照为 `failed/20011`、对象已清理且无 parse 投递。模拟数据库提交后进程退出，验证 `pending` 初始任务不会执行；未超过 300 秒的同键重放返回 `20008/409` 且无副作用，超时后重放或恢复扫描器按 `upload_batch_id` 接管，将完整批次切换为 `queued` 或收敛为 `failed/20011`。
6. 分别构造包含一个无效格式、一个超过 50MB 文件及总计 21 个文件的批次；均应整批拒绝，分别返回 `20009/400`、`20003/400`、`20004/400`，并验证没有创建任何资料、任务、幂等结果或正式文件对象。
7. 对故意损坏 PDF，验证上传先返回 `202`，随后资料和任务详情以 HTTP `200` 返回 `status=failed`、`error_code=20001` 和固定提示“资料解析失败，请删除后重新上传”；不得把异步错误伪装成上传阶段的 HTTP 400。
8. 分别上传扫描且无文本的 PDF、空 DOCX、仅空白 Markdown/TXT；验证其以 `20010 EMPTY_DOCUMENT` 和固定提示“资料内容为空，请删除后重新上传”收敛为 `failed`，且未创建草稿片段、嵌入或正式片段。验证 DOCX 压缩炸弹、路径穿越、宏/脚本、外部链接和解析超时均被安全拒绝。
9. 不带幂等键连续上传相同内容，验证创建两个独立资料；使用同一 `Idempotency-Key` 重放同一请求，验证返回首次结果且资料、任务、文件对象数量不增加；同键不同请求返回资源冲突。
10. 并发调度同一用户 4 份资料，验证同时最多 3 份处于 `processing`，且每份资料的名额跨 parse/chunk/embed/finalize 持续持有；其余保持 `pending/queued`。验证后台文档任务初次执行为 `attempt_no=1/retry_count=0`，`max_retries=3` 表示最多再重试 3 次且最多创建 4 个 attempt，达到预算后不再排队；每个新任务独立计数。验证 `documents.retry_count` 镜像当前阶段任务，阶段切换、新删除轮次和完成时按规则重置，且任务预算不受模型网关调用重试配置影响。模拟 `running` worker 失联，验证活动 attempt 被关闭、名额释放，并依据该重试预算恢复为 `queued` 或收敛为失败，且不存在两个活动 attempt。分别模拟阶段成功后的进程退出，验证当前阶段完成、下一阶段幂等创建、`current_task_type`、文档重试镜像与 `lease.task_id` 在同一事务一致，提交后投递丢失可由扫描器重投。
11. 在 embed 完成、finalize 尚未执行时验证 `chunks` 已幂等写入但所有业务检索返回零条；finalize 只校验数量/版本并翻转 completed。架构测试确认路由、服务和 worker 不存在绕过 `ChunkRepository` 的读取。
12. 对已完成资料提问；验证回答有来源引用。验证低于 `RETRIEVAL_VECTOR_MIN_SIMILARITY` 或
    `RETRIEVAL_TRGM_MIN_SIMILARITY` 的候选在 RRF 前被过滤；用不相关问题提问，使融合后为空，验证系统
    明确无证据、不创建 Citation 且不调用回答生成模型。
13. 对处理中、失败或已删除资料提问；验证其从检索中排除。首次 DELETE 递增 `delete_cycle`、创建清理任务并立即隐藏；资料处于 `deleting` 时所属用户重复 DELETE 返回成功，但轮次、任务和 attempt 数量不变；`delete_cleanup` 后保留 `deleted` 墓碑且 GET/DELETE 均返回 404。模拟 `delete_cleanup` 重试耗尽，验证资料以 `failed`、`current_task_type=delete_cleanup`、`error_code=20015` 和“资料删除未完成，请重试删除”仅向所属用户显示最小墓碑，`allowed_actions` 仅为 `retry_delete`；从该状态再次 DELETE 才递增轮次、新建清理任务并保留旧任务/attempt/retry_count。删除运行中的资料时，验证所有持久化仓储写入携带 `attempt_id` 并在同一事务检查 attempt/task 为 running 且 document 非 deleting；删除提交后的下一次写入被 fencing 拒绝，心跳也不得延长已冻结的 lease.expires_at。模拟 worker 卡死，验证扫描器到期后取消 attempt/task、释放名额并激活 `delete_cleanup`；running attempt 无活动 lease 时立即接管。删除已引用资料后，历史 Citation 的 ID 为空、`source_type=snapshot`，并返回保存的文件名、定位和内容预览。
    对非空知识库执行同样验证：任一子资料清理耗尽后知识库收敛为仅所属用户可见的 `delete_failed/20015` 最小墓碑，`allowed_actions` 仅为 `retry_delete`；再次 DELETE 才转回 `deleting` 并仅重试失败子资料，全部清理成功后物理删除。
14. 验证正常回答和可信无证据答复收敛为 `completed/stop`。注入供应商、模型与服务错误并耗尽重试，验证发送 `error` 事件且助手消息为 `failed/error`；断开流式连接，验证助手消息为 `cancelled/cancelled`。模拟 API 进程在 assistant 已创建后退出，待超过 `MESSAGE_STREAMING_STALE_SECONDS` 后运行维护扫描器，验证仅该仍为 `streaming` 的消息条件更新为 `failed/error`。原始响应必须是合法 `event:`/`data:` 文本帧，解码对象符合判别联合；用户消息始终为 `completed`，所有分支均不存在永久 `streaming`。
15. 使用 Bearer Access Token 与请求体 refresh token 调用登出；验证只撤销属于当前用户的持久化会话并写入 `revoked_at`，重复删除同一已撤销或已过期会话仍返回成功，无法匹配或跨用户 token 返回 `10006/401`。再用失效、撤销和重放的 refresh token 调用刷新接口；均应返回 `10006/401`，不得复用登录密码错误码，也不得撤销该用户的其他 active sessions；Redis 与日志中不得出现原始 token。
16. 对知识库、资料、任务、对话和引用列表验证 `page/page_size` 默认值、最大 100 和越界拒绝；资料公开 `status` 仅允许 `pending/queued/processing/completed/failed`，传入 `deleting/deleted` 必须返回 `10003/400`，不带或携带任何过滤都不能读取隐藏资料；对消息历史验证 `before/limit`、`has_more` 与 `next_before` 连续且无重复。
17. 使用 `openai-compatible` 适配器替身验证 endpoint、密钥和模型均由配置注入，且只有 ModelGateway
    执行各调用类型的超时和重试并返回最终成功或失败；业务用例适配器不得产生第二层尝试，但必须分别
    执行 Embedding 资料失败、改写原问题、Reranker 原 RRF、Generation `failed/error` 的领域降级；缺失/非法 endpoint
    或未知 provider 启动失败；Embedding 30 秒/2 次后当前资料失败；改写 10 秒/1 次后使用原问题；
    Reranker 对合法评分按 score 降序且同分保持 RRF 原顺序，对缺项、重复/越界序号、非有限 score 或
    非法 JSON 整体回退 RRF；10 秒/1 次后也使用 RRF；生成首 token 15 秒、总时长 120 秒、1 次后
    收敛为 `failed/error`；仅客户端连接断开收敛为 `cancelled/cancelled`，并且不生成无证据内容。
18. 验证分级限流默认值：认证接口同时按每 IP 20 次/5 分钟和每账号 5 次/5 分钟限制；上传
    每用户 10 次/10 分钟；问答每用户 20 次/分钟；其他认证接口每用户 120 次/分钟。超限响应
    必须为 `10005/429`，包含 `Retry-After` 和 `trace_id`，并且数据库与任务队列没有新增记录。
19. 在两个 API 实例间交替请求，验证 Redis 共享同一限流窗口；并发临界值测试不得多放行请求。
    模拟 Redis 不可用时，全部状态变更端点返回 `50001/503` 且无业务副作用；只读 GET 端点按
    配置降级时只记录不含主体原值和请求内容的元数据。
    可信代理列表为空时伪造 `X-Forwarded-For` 不得改变来源 IP；配置可信代理后，多跳链必须由右
    向左选择首个非可信地址；非法链或全为可信地址的链回退直连对端，完整转发链不得进入 Redis、日志或指标。
20. 使用受控假供应商验证 Embedding、Query Rewrite、Reranker 和生成全部经过内部出口网关；
    架构测试应拒绝业务服务、worker 或普通适配器直接导入供应商 SDK/客户端。
21. 向四类模型调用分别注入密码、令牌、请求头、邮箱、电话、身份证件号、内部标识和存储路径：
    验证禁止字段未外发、允许标识被替换为不可逆占位符；使脱敏器异常时验证没有任何外部网络
    请求，并按调用类型回退或收敛为明确终态。
22. 检查成功、重试、降级和失败的模型调用日志：只允许 `trace_id`、调用 ID、不可逆主体摘要、
    调用类型、供应商、模型、时间、耗时、状态、错误分类、重试次数、token 数量和载荷字节数；
    不得出现请求/响应正文、脱敏后正文或任何禁止字段。
23. 运行确定性的 RAG 功能示例测试：有召回证据时保存并返回字段完整、可定位的 Citation；无证据时可信拒答；删除来源后返回 snapshot。不得为 SC-001～SC-007 建立固定评测集、比例断言、性能阈值门禁或发布阻断条件。
24. 对每个非 SSE API 模拟未分类服务端异常；验证返回 `50000/500` 统一信封、UUID `trace_id` 和安全提示，且 OpenAPI 声明该响应。
25. 删除同时包含 queued、running 和 completed 资料的知识库；验证提交后知识库及子资源立即不可见，运行写入被 fencing 拦截，全部资料完成 `delete_cleanup` 后才物理删除知识库并级联对话、消息和引用，本地持久卷无孤儿对象。删除期间普通 GET/list/子资源始终不可见，但所属用户重复 DELETE 可命中：无失败子资料时幂等成功且任务数不变；模拟子资料以 `20015` 失败时，仅为失败子资料创建新删除轮次；成功物理删除后再次 DELETE 返回 404。

## 前端验证

1. 在后端契约测试全部通过后启动前端；仅通过 `/v1` API 与 SSE 调用服务。
2. 验证登录、本人基本资料查看/更新、知识库创建、批量上传、资料轮询、失败删除、会话创建、流式回答与来源抽屉。
3. 验证前端正确显示 50MB/20 文件限制、无证据提示、权限拒绝、生成失败和已取消消息；不得显示重处理或替换入口。

## 质量与交付验证

1. 后端依次执行 Ruff format/check、Pyright、pytest、迁移与扩展就绪检查、OpenAPI 校验。
   OpenAPI 校验必须确认所有操作声明限流策略，`10005/429` 响应具有必需的 `Retry-After`。
2. 前端依次执行 pnpm lint、Prettier check、TypeScript 类型检查、Vitest 和 Playwright。
3. 使用 Docker Compose 启动 PostgreSQL、Redis、后端 worker、后端和前端；验证健康与就绪检查。
   重建 API/worker 容器后必须验证 `/data/orionamesh` 命名卷中的原始资料和解析对象仍存在且可读取。
4. GitHub Actions 必须以锁文件安装依赖、执行上述质量门禁，并用 `GITHUB_TOKEN` 的
   `packages: write` 权限发布 `ghcr.io/${GITHUB_REPOSITORY}-backend` 与
   `ghcr.io/${GITHUB_REPOSITORY}-frontend`；仓库路径转小写，受保护分支使用不可变
   `sha-${GITHUB_SHA}`，正式 Git tag 可追加语义版本，部署不得使用 `latest`。
5. 架构门禁必须确认供应商 SDK 和外部模型 HTTP 客户端仅存在于
   `backend/app/infrastructure/model_gateway/providers/`；出口安全测试必须证明脱敏失败时无网络调用。
6. Compose 通过 `BACKEND_IMAGE`、`FRONTEND_IMAGE` 接收完整镜像引用。回滚必须把两者一起切换到
   上一已验证 SHA，执行 `docker compose pull`、`docker compose up -d` 和健康检查。镜像回滚不得
   自动降级数据库；破坏性迁移发布前必须人工备份，失败时停止发布并按备份恢复。
7. 部署升级必须先拉取镜像，再由单次串行 one-off 后端容器执行 `alembic upgrade head`；成功后才
   切换 API、worker 和前端并执行健康检查。API/worker 启动不得自动迁移，迁移失败时旧容器保持运行。
