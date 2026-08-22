# 数据模型：OrionaMesh 个人知识库 RAG MVP

> 本文件定义领域逻辑模型、核心字段、关系与不可变约束，不是完整可执行 DDL。`backend/app/models/`
> 中的 ORM 模型与 `backend/migrations/versions/` 中的 Alembic 迁移共同构成物理建表真相源，必须
> 实现并验证本文的字段、外键、索引、状态机、租户隔离与事务边界。
>
> 文档职责：本文件是领域数据边界、状态机、事务与不变量的唯一权威来源；用户需求以 `spec.md` 为准，公开传输 DTO 与错误码以 `contracts/openapi.yaml` 为准，运行默认值以 `quickstart.md` 为准。

## 共同规则

- 所有资源使用 UUID 主键、`created_at` 和必要的 `updated_at` 时间戳。
- 所有按租户查询的业务与派生记录保存 `user_id`，并在服务层和查询层按当前认证用户过滤。
- 文件和解析结果路径必须与具体存储后端无关；不得在业务记录中保存本地绝对路径。
- MVP 默认存储根目录为容器内 `/data/orionamesh` 本地持久卷；数据库只保存相对于该根目录的对象键，并阻止 `..`、绝对路径和符号链接逃逸。
- 当前可检索资料满足：资料状态为 `completed`，且片段的 `document_version` 等于资料当前 `version`。
- `chunks` 的所有读取必须经过统一 `ChunkRepository`；除迁移和测试夹具外，路由、服务与 worker 不得直接执行该表的 SQL 或 ORM 查询。

## 基础设施数据边界

### 请求限流计数（Redis，非领域实体）

- 限流窗口、计数和成员只保存在 Redis，设置不长于“窗口 + 清理余量”的 TTL；不得写入
  PostgreSQL，也不得被解释为用户、会话、任务或文档的业务状态。
- 限流键由策略版本、接口类别、窗口和主体摘要组成。认证账号及已认证用户标识必须使用带
  服务端秘密的不可逆摘要，禁止在 Redis 键或成员中保存邮箱、用户 UUID、令牌或来源请求内容。
- 来源 IP 仅用于认证防爆破计数；不得将其与资料内容、问题正文或模型调用正文组合存储。
- 来源 IP 默认使用 TCP 直连对端并忽略全部转发头。只有直连对端命中显式配置的可信代理 CIDR
  时，才解析 `X-Forwarded-For` 并由右向左选择首个非可信地址；任一地址格式非法或不存在非可信地址时回退直连
  对端。可信代理列表属于部署配置，不进入领域实体；完整转发链不得写入 Redis、日志或指标。
- 计数创建、清理和判断必须在单次原子操作中完成，响应只返回等待秒数，不暴露内部键。

### 模型出口调用上下文（进程内临时对象）

- 调用上下文包含调用 ID、`trace_id`、不可逆用户/租户摘要、调用类型、路由配置引用和最小必要
  内容；原始内容仅存在于完成脱敏所需的短生命周期内，不持久化为新的数据库实体。
- 供应商凭证只允许在网关发送边界从配置注入；不得进入领域模型、Celery 任务 payload、重试
  checkpoint、数据库、Redis 限流数据、异常消息或日志。
- 脱敏后的外发内容只用于本次网络调用，不写入调用日志；是否由现有业务实体保存回答或嵌入
  结果，仍遵循消息、片段和引用的既有数据模型。
- 模型调用审计使用结构化日志/指标，不新增存放 payload 的审计表。允许字段严格限于
  `trace_id`、调用 ID、不可逆主体摘要、调用类型、供应商、模型、时间、耗时、状态、错误分类、
  重试次数、token 数量和载荷字节数。

## 实体

### 用户（users）

| 字段 | 规则 |
|---|---|
| id | UUID，主键 |
| email | 必填、规范化后唯一；先去除首尾 Unicode 空白、完成邮箱格式校验，再对完整邮箱执行 Unicode `casefold`。注册冲突、登录查找和账号限流 HMAC 必须复用同一函数 |
| password_hash | 必填；不得保存明文密码。注册前的明文密码必须至少 8 字符并同时含字母和数字；确认密码仅在客户端校验，不入 API 或数据库 |
| display_name | 可选，最多 100 字符 |
| last_login_at | 可空；注册时为空，仅在首次及后续登录成功时更新 |

### 登录会话（auth_sessions）

| 字段 | 规则 |
|---|---|
| user_id | 必填，引用用户 |
| refresh_token_hash | 必填且唯一；明文固定为 `rt_` + 32 字节 CSPRNG 随机值的无填充 Base64URL 编码（总长 46），仅返回客户端一次；服务端只保存 SHA-256 摘要，不编码为 JWT |
| rotated_from_session_id | 可选，记录轮换来源 |
| expires_at / revoked_at | 判断有效会话；撤销为持久化事实 |

- 刷新轮换事务按 `refresh_token_hash` 锁定旧 session，并在锁内复查未撤销、未过期后才撤销旧记录和
  创建单一后继；同一 token 的并发后到请求在锁内观察到已撤销后返回 `10006/401`。

### 知识库（knowledge_bases）

| 字段 | 规则 |
|---|---|
| user_id | 必填；所有访问以其为边界 |
| name / normalized_name | `name` 是用户显示名称，最多 120 字符；`normalized_name` 为去除首尾 Unicode 空白后执行 Unicode `casefold` 的内部值，不对外返回。`status=active` 时二者均非空，数据库以部分唯一索引保证 `(user_id, normalized_name)` 唯一；`deleting` 与 `delete_failed` 不占用该唯一性，以便用户重新使用名称 |
| description | 可选 |
| status | `active`、`deleting` 或 `delete_failed`；`deleting` 从列表、详情、对话创建和检索中隐藏；`delete_failed` 在所属用户的知识库列表/详情中仅返回最小“删除未完成”墓碑和 `retry_delete`，不得暴露普通知识库内容或子资源。仅所属用户的 DELETE 命令可通过独立变更查询命中这三种状态，该查询不得复用于普通内容读取。 |
| delete_error_code | 可空；仅 `delete_failed` 时固定为 `20015`，用于表达子资料删除清理未完成 |
| allowed_actions | 非持久化响应字段；`active` 为 `delete`，`delete_failed` 仅为 `retry_delete` |

- 创建或改名必须在同一事务中写入 `normalized_name` 并依赖上述部分唯一索引处理并发；唯一冲突映射为
  `20016/409`，不得先以应用层预查询替代数据库约束。

### 资料（documents）

| 字段 | 规则 |
|---|---|
| user_id / knowledge_base_id | 必填；与知识库所有者一致 |
| filename / file_type / file_size | 文件元数据；类型限 pdf、docx、md、txt；单文件最大 50MB |
| storage_path | 存储后端无关的原始资料路径 |
| upload_batch_id | 必填内部 UUID；标识同一次整批上传，供文件转正协调、补偿和崩溃恢复使用，不对外作为业务资源 ID |
| content_hash | 规范化内容哈希，用于完整性校验和诊断；不唯一，不得据此合并用户重复上传的资料 |
| status | 创建进入 `pending`，经 `queued → processing → completed` 发布；`parse/chunk/embed/finalize` 重试耗尽从 `processing` 进入 `failed`。任一未删除资料可进入 `deleting`；删除清理成功进入 `deleted`，清理重试耗尽进入 `failed` 且以 `current_task_type=delete_cleanup` 区分。 |
| version | MVP 创建时为 1；保留字段，不开放用户重处理或替换 |
| current_task_type / retry_count | 用户可解释的当前处理阶段及其任务重试次数；`retry_count` 镜像当前任务，初次执行为 0，每次重试递增，阶段切换或新删除轮次重置为 0，完成且阶段为空时为 0，不做全流水线累计；`failed/delete_cleanup` 是删除未完成专属状态 |
| delete_cycle | 删除清理轮次，初始为 0；首次删除及 `failed/delete_cleanup` 后重试时递增，`deleting` 状态幂等重放不递增，供审计与运维告警使用 |
| error_code / error_message | 失败时持久化稳定业务错误码与固定安全提示：`20001`“资料解析失败，请删除后重新上传”、`20010`“资料内容为空，请删除后重新上传”、`20011` 文件持久化失败、`20012` 嵌入失败、`20013` 发布校验失败、`20014` 无法归类的重试耗尽、`20015`“资料删除未完成，请重试删除”；未知内部错误使用 `50000` |
| chunk_count | 当前完成版本的正式片段数量；`finalize` 校验成功后更新 |
| processing_started_at / processing_finished_at | 首次进入处理与最终收敛时间；用于诊断和用户展示 |
| allowed_actions | 非持久化响应字段，由服务端按状态计算；普通资料仅允许 `delete`，`failed/delete_cleanup/20015` 仅允许 `retry_delete`；两者均不得暗示重处理能力 |

### 资料处理任务与尝试（document_tasks / document_task_attempts）

| 字段 | 规则 |
|---|---|
| user_id / knowledge_base_id / document_id / document_version | 必填，支持租户和版本过滤 |
| task_type | `parse`、`chunk`、`embed`、`finalize`、`cleanup`、`delete_cleanup`；`cleanup` 仅清理旧版本，`delete_cleanup` 仅清理被删除资料 |
| delete_cycle | 非删除任务固定为 0；`delete_cleanup` 必须大于 0，并复制资料当前轮次。从 `failed/delete_cleanup` 重试删除时新建任务，`deleting` 状态重放不建任务；历史任务及 attempt 不得修改。 |
| status | `pending`、`queued`、`running`、`succeeded`、`failed`、`cancelled`；失联恢复可将 `running` 原子恢复为 `queued`，重试耗尽必须进入明确终态 |
| retry_count / max_retries | 后台文档任务调度级失败重试控制；初次 `attempt_no=1/retry_count=0`，每次重试先递增计数再创建 attempt；MVP 的 `max_retries=3` 表示初次执行外最多重试 3 次，单任务最多 4 个 attempt，达到预算后不得再排队；每个新任务独立计数，与模型网关调用重试相互独立 |
| total_items / processed_items | 可为空的总量与已处理量；不得据此替代任务状态 |
| queued_at / started_at / finished_at | 排队、执行和终态时间；用于恢复与诊断 |
| attempt 记录 | `attempt_no` 从 1 开始且单任务严格递增；`document_task_attempts` 冗余保存父任务的 `user_id`、`knowledge_base_id`、`document_id`、`document_version`，均不可为空；父任务以 `(id, user_id, knowledge_base_id, document_id, document_version)` 建立复合唯一键，attempt 以 `(task_id, user_id, knowledge_base_id, document_id, document_version)` 建立复合外键，数据库强制四个冗余边界与同一父任务完全一致。创建时仍在同一事务锁定父任务并复制、校验一致，后续不得修改。attempt 必须记录 `worker_name`、非空 `started_at`、可空 `finished_at`、可空 `error_message` 和仅结束后可计算的可空 `duration_ms`；这些字段均通过公开 DTO 返回。attempt ID 同时作为持久化写入的 fencing token；同一任务最多一个未结束 attempt；失联时先关闭 attempt 再重新排队、失败或取消；attempt 的读写必须经带当前 `user_id` 条件的任务尝试仓储完成 |
| idempotency_key | 阶段任务幂等键；普通阶段使用 `{task_type}:{document_id}:v{document_version}`，删除清理使用 `delete_cleanup:{document_id}:v{document_version}:d{delete_cycle}`，与批量上传请求的 `Idempotency-Key` 作用域不同 |
| error_code / error_message | 任务失败时的稳定业务错误码与安全摘要；通过任务详情的 `200` 响应返回，不作为异步阶段的 HTTP 状态码 |

### 上传幂等记录（document_upload_requests）

| 字段 | 规则 |
|---|---|
| user_id / knowledge_base_id | 必填；定义幂等作用域并纳入租户过滤 |
| idempotency_key | 客户端提供的上传重放键；与 `user_id + knowledge_base_id` 组成唯一键 |
| request_fingerprint | 文件数量、名称、大小与内容摘要形成的不可逆指纹；同键不同请求必须返回冲突，不得复用首次结果 |
| response_snapshot | 首次接受结果的资料 ID 列表和必要状态；文件全部转正或补偿失败时必须与资料/任务状态在同一事务更新为 `queued` 或 `failed/20011`；不得包含文件正文或凭证 |
| status / upload_batch_id | `coordinating`、`accepted` 或 `failed`；关联内部批次，使同键重放能区分正在协调、已收敛和可接管的超时请求 |
| expires_at | 幂等保留期，默认 24 小时；过期记录由现有 Celery Beat 恢复/维护扫描任务批量删除 |

### 处理并发名额（document_processing_leases）

| 字段 | 规则 |
|---|---|
| user_id / document_id / task_id | `user_id`、`document_id` 必填；名额归属于整份资料处理流水线，`task_id` 仅记录当前执行阶段归属，可随阶段切换更新但不得触发释放再获取 |
| acquired_at / heartbeat_at / expires_at | 用于诊断、续租和失联回收；删除事务锁定 lease 并以当时的 `expires_at` 冻结等待上限，资料进入 `deleting` 后心跳不得再续租，默认租期最长 300 秒 |
| released_at / release_reason | 记录完成、失败、取消、删除或恢复回收；释放后不可再次激活 |
| 唯一性 | 每个资料最多一个未释放名额；获取前在数据库事务内按 `user_id` 加锁并统计未释放名额 |

### 解析结果与片段（document_parse_results / document_chunk_drafts / chunks）

| 字段 | 规则 |
|---|---|
| user_id / knowledge_base_id / document_id / document_version | 必填；派生表保留租户与版本边界 |
| 解析结果 | 包含相对内容对象键、内容哈希、解析器名称/版本、标准化字符数与结构元数据；标准化文本为空时不得创建成功结果 |
| 草稿片段 | 仅供流水线中间阶段使用，不得参与检索 |
| 正式片段 | `embed` 按幂等批次直接写入 `chunks`；在 `finalize` 校验成功并将资料翻转为 `completed` 前属于未发布数据，不得参与任何业务读取或检索；包含内容、顺序、嵌入、模型/策略版本、页码与章节元数据 |
| 唯一性 | 文档、版本、分块策略、嵌入模型和顺序组成唯一逻辑键 |

### 对话与消息（conversations / messages）

| 字段 | 规则 |
|---|---|
| conversation.user_id / knowledge_base_id | 均必填；知识库必须属于当前用户，MVP 不提供纯聊天对话 |
| conversation.title / last_message_at | 标题和最后消息时间均可为空；无消息的新对话允许尚未生成标题 |
| message.user_id / conversation_id | 均必填；`user_id` 为防御性租户边界，必须与对话所有者一致 |
| message.role | `user` 或 `assistant` |
| message.status | 用户消息为 `completed`；助手消息正常完成或可信无证据答复为 `completed/stop`，供应商、模型或服务错误重试耗尽为 `failed/error`，客户端连接断开为 `cancelled/cancelled`。API 进程失联时，维护扫描器必须以 `status=streaming AND created_at < now() - MESSAGE_STREAMING_STALE_SECONDS` 为条件原子更新为 `failed/error`；所有分支必须离开 `streaming` |
| message.finish_reason | 用户消息为空；助手消息与终态严格配对：`completed` 仅为 `stop/length`，`failed` 仅为 `error`，`cancelled` 仅为 `cancelled`，`streaming` 时为空 |
| content / rewritten_query | 内容及可选改写查询；查询改写只使用当前对话最近三轮上下文 |

### 回答引用（message_citations）

| 字段 | 规则 |
|---|---|
| message_id / user_id / knowledge_base_id | 必填，先验证消息所属对话及当前用户 |
| chunk_id / document_id | 当前来源可访问时必填；资料删除或来源不可访问而回退快照时为空 |
| document_version / rank / score | 均必填；保存当时证据版本与排序信息，`rank >= 1` 且同一消息内唯一 |
| chunk_snapshot | 必填 JSON；保存文件名、文件类型、页码、章节和内容预览；只供历史核验，不可恢复原始资料 |
| source_type | 非持久化响应字段：`live` 表示当前可访问来源，`snapshot` 表示删除后快照 |

## 关键关系与事务

```text
用户 1 ── N 知识库 1 ── N 资料 1 ── N 任务 / 解析结果 / 草稿片段 / 正式片段
用户 1 ── N 对话 1 ── N 消息 1 ── N 回答引用
```

- 批量上传：先对整批格式、单文件大小和文件数量做无副作用预校验；任一失败则拒绝整批。全部通过后生成 `upload_batch_id`，把临时对象写入可由该批次和资料 ID 推导的对象键，并在同一数据库事务中创建全部 `pending` 资料、不可执行的 `pending` 初始 `parse` 任务和可选幂等结果；数据库失败时回滚、清理临时对象并返回 `50000/500`。全部对象转正后，在一个事务中将整批资料、任务和幂等响应快照切换为 `queued`，随后投递 parse 并返回 `202`；任一对象转正失败时清理本批临时及已转正对象，在补偿事务中将三者全部置为 `failed/20011`，不投递 parse，并以 `202` 返回失败资料。
- 上传恢复：`documents.status=pending` 且初始 parse 任务为 `pending` 表示文件转正尚未协调完成，worker 不得直接执行。API 协调器按 `upload_batch_id` 对整批 documents 执行 `SELECT ... FOR UPDATE SKIP LOCKED`，在持有该短事务行锁期间完成同一本地卷的原子重命名、更新活动时间并最终切换状态。恢复扫描器获取同一批次锁后，必须再次确认已超过 `DOCUMENT_UPLOAD_PENDING_TIMEOUT_SECONDS`（默认 300 秒）才可接管；锁不可得时跳过，进程崩溃后行锁自动释放。批内每份资料已有正式对象或仍有完整临时对象时，幂等完成剩余转正并原子切换为 `queued`；任一资料的正式与临时对象都缺失、损坏或转正失败时整批补偿为 `failed/20011`。
- 上传重放：同一 `user_id + knowledge_base_id + Idempotency-Key` 命中 `accepted/failed` 请求时返回首次已收敛快照，不重复创建资料、任务或文件对象；命中未超时 `coordinating` 请求时返回 `20008/409` 且不产生副作用；命中超时请求时获取批次锁并调用与扫描器相同的幂等协调函数接管。未提供该键时，每次上传都创建独立资料。
- 处理并发：资料首次进入 `processing` 时通过数据库事务按 `user_id` 原子获取最多 3 个（可配置）的资料级名额；名额跨 parse、chunk、embed、finalize 持有。恢复扫描器对超时 lease 与 `running` 任务加锁，关闭活动 attempt 并释放名额；有重试预算则把任务恢复为 `queued`，否则使任务和资料明确失败。Redis/Celery 不得作为名额真相源。
- 阶段切换：worker 不得自行拼接下一阶段。统一编排器在一个事务内锁定并校验当前 attempt、task、document 和 lease，把 attempt/task 标为 `succeeded`，按阶段幂等键创建或激活下一任务，更新 `documents.current_task_type` 与 `lease.task_id` 后提交；只在提交后投递 Celery。投递失败时下一任务仍为数据库中的 `queued`，由恢复扫描器重投。`finalize` 成功则在同一事务完成资料状态翻转并释放处理名额，旧版本 `cleanup` 独立排队且不影响当前版本可见性。
- 解析边界：PDF/DOCX/MD/TXT 分别使用已锁定的解析器；不得执行宏、脚本、外部链接或嵌入对象，并限制解析时长、归档条目数和解压后大小。标准化文本为空时资料和任务以 `20010` 收敛为 `failed`。
- 片段读取：`ChunkRepository` 提供两类显式方法；检索方法必须 `JOIN documents` 并过滤当前 `user_id`、`knowledge_base_id`、`documents.status = completed` 和 `chunks.document_version = documents.version`；流水线内部计数/校验方法必须过滤当前 `user_id`、`knowledge_base_id`、`document_id` 和精确 `document_version`，不得复用于用户查询。
- 证据门槛：`ChunkRepository` 的向量检索仅返回余弦相似度不低于 `RETRIEVAL_VECTOR_MIN_SIMILARITY`（默认 `0.65`）的候选；关键词检索仅返回 pg_trgm 相似度不低于 `RETRIEVAL_TRGM_MIN_SIMILARITY`（默认 `0.30`）的候选。RRF、重排和 Context Pack 只能消费通过门槛的候选；融合后为空时问答服务直接可信拒答，不调用生成模型。
- `finalize`：不搬运或复制片段；仅通过流水线内部仓储方法校验正式片段数、版本和任务结果一致后，把资料标记为 `completed` 并更新 `chunk_count`；否则以 `20013` 将任务和资料收敛为失败，未发布片段仍不可被检索。
- 持久化写入 fencing：解析结果、草稿片段、正式 `chunks`、checkpoint 与阶段结果引用的仓储写方法必须接收 `attempt_id`。每次写入在同一数据库事务中锁定 attempt、task 和 document，并校验 attempt/task 均为 `running`、版本一致且资料不为 `deleting/deleted`；条件不满足则整笔写入失败并将 worker 导向取消收敛。外部模型调用不得包含在该事务内；对象正文可先写临时键，只有正式对象引用的数据库写入通过 fencing 后才算提交。
- 删除资料：DELETE 使用独立且强制 `user_id` 的锁定变更查询，可命中普通可见资料、`deleting` 与 `failed/delete_cleanup/20015`，不得复用于 GET/list。首次删除才把资料标为 `deleting`、取消未开始任务、递增 `delete_cycle` 并新建专用 `delete_cleanup`；命中 `deleting` 时幂等成功且不递增轮次、不创建任务；命中 `failed/delete_cleanup/20015` 时递增轮次并新建任务；`deleted` 返回 404。从首次事务提交起列表、详情和检索均隐藏资料。没有活动 attempt 时立即释放 lease 并激活 `delete_cleanup`；存在活动 attempt 时锁定并保留 lease，以事务当时的 `expires_at` 冻结等待上限，后续心跳因资料已 deleting 而不得续租。若 worker 失联，孤儿任务扫描器在租约超时后于同一事务锁定 attempt/task/document/lease，把 attempt 与 task 置 `cancelled`、释放 lease 并激活 `delete_cleanup`。若存在 running attempt 却没有活动 lease，则视为已失联并立即执行相同接管。`delete_cleanup` 清理原始文件、解析结果、草稿和正式片段后保留最小墓碑并标为 `deleted`；若清理重试耗尽，则资料转为 `failed`、`current_task_type=delete_cleanup`、`error_code=20015`，仅向所属用户显示最小“删除未完成”墓碑和 `retry_delete`。从该失败状态重试删除不得重置旧任务、attempt 或其 retry_count；历史引用行保留，外键置空并通过必填快照核验来源。
- 删除知识库：DELETE 事务先将知识库标记为 `deleting`，并为其每份资料执行上述删除编排；知识库及全部子资源从提交起不可见。知识库列表/详情以所属用户为范围返回 `active` 完整对象或 `delete_failed` 最小墓碑；对话、资料等内容读取只命中 `active`。删除命令使用独立、带 `user_id` 的锁定查询命中 `active/deleting/delete_failed`。命中 `deleting` 时幂等成功且不创建任务；任一子资料进入 `failed/delete_cleanup/20015` 后，维护扫描器将知识库置为 `delete_failed`、`delete_error_code=20015`，仅返回最小墓碑和 `retry_delete`。从 `delete_failed` 再次 DELETE 才转回 `deleting`，且仅为失败子资料创建新的删除轮次。所有资料均为 `deleted` 且无活动 attempt 后，维护扫描器才物理删除知识库并级联对话、消息及引用；空知识库可立即物理删除，之后再次 DELETE 返回 404。不得使用立即 `ON DELETE CASCADE` 作为运行 worker 和持久化文件的清理机制。删除单条对话仍级联消息和引用。
- 索引边界：消息至少建立 `(user_id, conversation_id, created_at)`；引用至少建立 `(user_id, message_id)` 与 `(user_id, knowledge_base_id, rank)`，任何引用查询仍必须先验证当前用户和对话归属。
- 重处理/替换：MVP 不提供端点或用户操作；仅保留 `version`、派生版本和清理结构以便 Phase 2 扩展。
- 外部模型出口：业务实体和任务只向内部模型网关提交最小必要内容；网关脱敏失败时不生成
  外部请求，并按调用类型执行回退或将现有资料/消息状态收敛为明确终态，不创建半成品审计记录。
