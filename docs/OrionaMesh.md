# OrionaMesh

一个工程级的 C 端 RAG 开源应用。

OrionaMesh 面向个人和轻量团队场景，核心目标是把本地或私有资料构建成可检索、可追溯、可持续更新的个人知识库，并在对话中提供带引用来源的回答。

## 产品设计

### 产品定位

- **项目形态**：开源应用，不是纯基础设施库。
- **核心场景**：用户上传 PDF、DOCX、Markdown、TXT 等文本资料，构建个人知识库，并基于知识库连续问答。
- **核心原则**：用户级数据隔离、异步文档处理、检索可追溯、历史对话可复查。

### 产品路径

1. 用户上传 PDF、DOCX、Markdown、TXT 等文本类资料，构建用户级隔离的知识库
2. 参考知识库的情况下，用户可以连续对话

### 产品功能

- 支持用户批量上传、查看和删除资料；MVP 不支持覆盖、编辑或重新处理源文件
- 支持连续对话，并保存历史对话

## 技术架构

### 项目约束

### 实现方案

#### 知识库构建

文档处理采用异步流水线，避免上传接口阻塞，也避免解析成功但 embedding 失败时出现不可追踪的脏状态。

```text
upload
  -> parse
  -> chunk
  -> embed
  -> finalize
  -> cleanup
```

1. `upload`：保存原始文件，创建 `documents` 记录，状态进入 `pending` / `queued`
2. `parse`：从原始文件抽取标准化文本和结构化 metadata
3. `chunk`：基于解析结果生成 `document_chunk_drafts` 草稿分块
4. `embed`：批量生成 embedding，写入正式 `chunks`
5. `finalize`：回写 `documents.status = completed`、`chunk_count`、处理结束时间
6. `cleanup`：异步清理旧版本派生数据，不影响当前版本检索正确性

任务执行必须通过 `document_tasks` 记录阶段状态；重试和排障信息可通过 `document_task_attempts` 记录。

#### 检索

1. 查询改写
2. 双路召回，使用向量查询和关键词查询
3. RRF 融合排序
4. 重排序
5. 上下文打包，整合、去重、压缩上下文
6. 输出内容

检索入口必须以 `conversation.user_id` 和 `conversation.knowledge_base_id` 为准。所有向量召回和关键词召回都以 `chunks c` 为主表，并且必须 `JOIN documents d ON d.id = c.document_id` 后强制携带：

```sql
WHERE c.user_id = $1
  AND c.knowledge_base_id = $2
  AND c.document_version = d.version
  AND d.status = 'completed'
```

其中 `$1` 来自当前登录用户，`$2` 来自当前会话绑定的知识库，不能直接信任前端传入的任意知识库 ID。检索 SQL 必须 `JOIN documents d ON d.id = c.document_id`，以 `documents.version` 作为当前可检索版本的真相源。

#### RAG 策略默认值

MVP 阶段保持 Top-K、RRF、Context Pack 等检索策略的固定默认值；证据门槛（`RETRIEVAL_VECTOR_MIN_SIMILARITY`、`RETRIEVAL_TRGM_MIN_SIMILARITY`）允许通过配置覆盖。

|环节|MVP 默认策略|说明|
|---|---|---|
|Query Rewrite|结合最近 3 轮历史消息，用低成本 LLM 改写|只处理省略指代、上下文补全，不主动扩展问题范围|
|向量召回|Top-K=10，余弦相似度 ≥ 0.65|低于阈值的候选不进入 RRF；阈值可配置|
|关键词召回|Top-K=10，pg_trgm 相似度 ≥ 0.30|低于阈值的候选不进入 RRF；`pg_jieba` / `zhparser` 作为可选增强|
|融合排序|RRF|合并双路召回结果，按 `chunk_id` 去重|
|Reranker|MVP 提供可选 API/本地适配器，但默认部署不要求启用|模型未配置或调用失败时直接使用 RRF 结果；默认启用与质量调优延期至 Phase 2|
|Context Pack|最多 3000 tokens，最终 5-8 个 chunks|按 rerank score 优先，相邻 chunk 可合并|
|生成|SSE 流式输出|回答必须基于检索上下文，无法从上下文确认时明确说明|

#### Context Pack 规则

1. 先按 rerank score 从高到低选择候选 chunk。
2. 对同一文档、相邻 `chunk_index` 的 chunk 优先合并，减少上下文割裂。
3. 同一 `document_id + document_version + chunk_index` 只保留一次。
4. 超过 token budget 时按优先级截断，保留引用 metadata。
5. 最终传给生成模型的上下文必须保留 `chunk_id`、`document_id`、`document_version`、`filename`、`page`、`section`，用于写入 `message_citations`。

#### 召回实施约束

- MVP 必须同时具备向量召回和关键词兜底召回；不能只依赖单一路径。
- `pg_trgm` 是 MVP 必备扩展，并且部署检查必须验证扩展存在。
- `pg_jieba` / `zhparser` 是可选增强，只能放在独立可选迁移中，不能阻塞基础迁移。
- 向量检索必须 `JOIN documents`，并强制过滤 `c.user_id`、`c.knowledge_base_id`、`c.document_version = d.version`、`d.status = 'completed'`。这保证租户隔离和版本正确性，但 HNSW 与过滤条件结合后可能出现性能波动。
- MVP 数据量较小时可以先使用全局 HNSW + tenant filter；如果单库 chunks 明显增长，优先演进为按 `knowledge_base_id` 分区或独立向量集合。
- 两路候选必须先分别通过相似度门槛，再进入 RRF；融合后为空时，不应直接生成普通回答或调用生成模型，应返回“知识库中未找到相关内容”，并允许用户换问法或检查文档处理状态。`RETRIEVAL_VECTOR_MIN_SIMILARITY` 默认 `0.65`，`RETRIEVAL_TRGM_MIN_SIMILARITY` 默认 `0.30`，均可通过配置覆盖。

#### 技术栈

#### Front-end

- Next.js
- React
- Ts
- Tailwind
- Shadcn
- pino

#### Back-end

- Langchain
- Pydantic
- FastAPI
- Redis
- Postgre
- Celery
- pgvector
- JWT
- structlog

#### Deploy

- Docker
- GitHub Actions
- GitHub Container Registry（GHCR）

### 数据模型

#### 概览（实体关系）

```
users
  └── auth_sessions (1:N)

users
  └── knowledge_bases (1:N)
        └── documents (1:N)
              ├── document_parse_results (1:1，解析结果)
              ├── document_tasks (1:N，异步处理任务)
              │     └── document_task_attempts (1:N，任务尝试记录)
              ├── document_chunk_drafts (1:N，中间态分块)
              └── chunks (1:N，已嵌入分块；仅完成态当前版本可见)

users
  └── conversations (1:N)
        └── messages (1:N)
              └── message_citations (1:N，关联 chunks + documents)
```

---

##### 1. users — 用户表

```sql
CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    display_name  VARCHAR(100),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at  TIMESTAMPTZ
);
```

|字段|类型|说明|
|---|---|---|
|id|UUID|主键|
|email|VARCHAR(255)|唯一，登录凭证；服务端先去除首尾 Unicode 空白、完成格式校验，再对完整邮箱执行 Unicode `casefold`。注册冲突、登录查询和账号限流 HMAC 必须复用该唯一规范化函数|
|password_hash|VARCHAR(255)|bcrypt 哈希|
|display_name|VARCHAR(100)|展示名称，可为空|
|created_at / updated_at|TIMESTAMPTZ|创建/更新时间|
|last_login_at|TIMESTAMPTZ|可为空；注册时为空，最近一次登录成功后更新|

---

##### 1.1 auth_sessions — 登录会话表

```sql
CREATE TABLE auth_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    refresh_token_hash VARCHAR(255) NOT NULL UNIQUE,
    rotated_from_session_id UUID REFERENCES auth_sessions(id) ON DELETE SET NULL,
    user_agent TEXT,
    ip_address INET,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_auth_sessions_user_id
ON auth_sessions(user_id);

CREATE INDEX idx_auth_sessions_active
ON auth_sessions(user_id, expires_at)
WHERE revoked_at IS NULL;
```

|字段|类型|说明|
|---|---|---|
|refresh_token_hash|VARCHAR|refresh token 只存哈希，不存明文|
|rotated_from_session_id|UUID|刷新 token 时记录轮换来源，便于发现重放|
|expires_at|TIMESTAMPTZ|refresh token 过期时间|
|revoked_at|TIMESTAMPTZ|登出或风控时置为当前时间|
|last_used_at|TIMESTAMPTZ|最近一次刷新时间|

**Token 策略：**

- access token 固定使用 `HS256` JWT，包含 `sub`、`iat`、`exp` 与 `type=access`，有效期 2 小时（7200 秒）；签名密钥由必填 `AUTH_JWT_SECRET_KEY` 注入，UTF-8 编码后至少 32 字节，验证端只允许 `HS256`，缺失或不合规时应用不得报告就绪。Access Token 缺失、Bearer 格式错误、签名或算法无效、必填声明错误、`type` 错误或过期均统一返回 `10001/401` 与“请重新登录”；`10004/401` 仅用于登录邮箱或密码不匹配。
- refresh token 固定使用 32 字节 CSPRNG 随机值，经无填充 Base64URL 编码并加 `rt_` 前缀（总长 46 字符）；明文只返回客户端一次，服务端仅在 `auth_sessions.refresh_token_hash` 保存其 SHA-256 摘要，不编码为 JWT。
- `PUT /auth/sessions` 刷新时必须在同一数据库事务中按 `refresh_token_hash` 锁定旧 session；只有锁定后仍未撤销且未过期的 session 才能轮换。事务内将旧 session 标记 `revoked_at`、创建新 session，并通过 `rotated_from_session_id` 关联；同一 refresh token 的并发后到请求在取得锁后必须因旧 session 已撤销而返回 `10006/401`，不得创建第二个后继 session。
- `DELETE /auth/sessions` 登出时按当前 refresh token 找到 session 并置 `revoked_at`。
- 如果已撤销 session 的 refresh token 再次被使用，视为疑似重放；MVP 仅拒绝本次刷新并返回
  `10006/401 INVALID_REFRESH_TOKEN`，不得连带撤销该用户的其他 active sessions。

---

##### 2. knowledge_bases — 知识库表

```sql
CREATE TABLE knowledge_bases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        VARCHAR(120) NOT NULL,
    description TEXT,
    status      VARCHAR(20) NOT NULL DEFAULT 'active', -- active | deleting | delete_failed
    delete_error_code INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_kb_status CHECK (status IN ('active', 'deleting', 'delete_failed')),
    CONSTRAINT ck_kb_delete_error CHECK (
        (status = 'delete_failed' AND delete_error_code = 20015)
        OR (status <> 'delete_failed' AND delete_error_code IS NULL)
    )
);

CREATE INDEX idx_kb_user_id ON knowledge_bases(user_id);
```

|字段|类型|说明|
|---|---|---|
|id|UUID|主键|
|user_id|UUID|外键，用户隔离核心字段|
|name|VARCHAR(120)|知识库名称，最多 120 字符|
|description|TEXT|描述，可为空|
|status|VARCHAR(20)|`active` / `deleting` / `delete_failed`；`deleting` 从列表和详情隐藏，`delete_failed` 在所属用户知识库列表/详情中仅返回最小删除失败墓碑，子资源仍不可见|
|delete_error_code|INTEGER|仅 `delete_failed` 时为 `20015`，否则为空|

> **用户隔离策略**：所有查询必须携带 `user_id` 条件。行级安全（RLS）可在后期启用。

---

##### 3. documents — 文档表

```sql
CREATE TABLE documents (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename          VARCHAR(500) NOT NULL,
    file_type         VARCHAR(20) NOT NULL,   -- pdf | docx | md | txt
    file_size         BIGINT NOT NULL,         -- 字节
    storage_path      TEXT NOT NULL,           -- 文件存储路径（MVP 为本地磁盘相对路径）
    upload_batch_id   UUID NOT NULL,           -- 内部上传批次，用于整批转正、补偿和崩溃恢复
    content_hash      VARCHAR(128),             -- 完整性/诊断用途，不唯一，不作为重复上传去重键
    status            VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- pending | queued | processing | completed | failed | deleting | deleted
    error_code        INTEGER,                 -- 异步失败业务码：20001 / 20010～20015 / 50000
    error_message     TEXT,
    chunk_count       INTEGER DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 1,
    current_task_type VARCHAR(32),
    retry_count       INTEGER NOT NULL DEFAULT 0,
    delete_cycle      INTEGER NOT NULL DEFAULT 0 CHECK (delete_cycle >= 0),
    processing_started_at  TIMESTAMPTZ,
    processing_finished_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_doc_kb_id   ON documents(knowledge_base_id);
CREATE INDEX idx_doc_user_id ON documents(user_id);
CREATE INDEX idx_doc_status  ON documents(status);
CREATE INDEX idx_doc_upload_batch ON documents(user_id, upload_batch_id, status);
```

|字段|类型|说明|
|---|---|---|
|status|VARCHAR|`pending` → `queued` → `processing` → `completed` / `failed`；删除为 `deleting` → `deleted`，删除清理重试耗尽则回到 `failed` 并以 `current_task_type=delete_cleanup` 区分|
|storage_path|TEXT|原始文件存储路径，MVP 为本地磁盘相对路径，例如 `uploads/{user_id}/{document_id}/source.pdf`|
|upload_batch_id|UUID|内部上传批次标识，用于整批文件转正、补偿和恢复；不作为外部业务资源 ID|
|content_hash|VARCHAR|完整性与诊断哈希；不唯一，内容相同的文件仍是独立资料|
|error_code / error_message|INTEGER / TEXT|异步失败的持久化业务码与安全提示；详情轮询仍返回 HTTP 200|
|chunk_count|INTEGER|入库成功后回写，方便展示|
|version|INTEGER|文档内容版本，重新上传、编辑或重建分块时递增|
|current_task_type|VARCHAR|当前处理阶段：`parse` / `chunk` / `embed` / `finalize`；删除清理失败时为 `delete_cleanup`|
|retry_count|INTEGER|镜像 `current_task_type` 对应任务已启动的重试次数，不做全流水线累计；初次执行为 0，每启动一次重试递增 1，切换到下一阶段或新建删除轮次时重置为 0，完成且 `current_task_type=null` 时为 0|
|delete_cycle|INTEGER|已发起的删除清理轮次；首次删除及 `failed/delete_cleanup` 后重试时递增，`deleting` 状态幂等重放不递增，用于审计和运维告警|
|processing_started_at / processing_finished_at|TIMESTAMPTZ|处理开始/结束时间，用于耗时统计|

> `documents.status` 是用户视角状态，适合前端列表和详情轮询；任务内部阶段以 `document_tasks.status` 为准。`current_task_type` 表示当前或即将执行的流水线阶段，因此文档刚创建且状态仍为 `pending` / `queued` 时可以显示为 `parse`；`completed` 后置为 `null`；普通处理失败时保留失败阶段。若 `failed` 且 `current_task_type = delete_cleanup`，则表示删除未完成：仅向资料所属用户暴露最小墓碑和“重试删除”操作，不得按普通处理失败资料展示。

---

##### 3.1 document_parse_results — 文档解析结果表

```sql
CREATE TABLE document_parse_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    document_version INTEGER NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    content_path TEXT NOT NULL,
    content_hash VARCHAR(128) NOT NULL,
    metadata JSONB DEFAULT '{}',
    parser_name VARCHAR(64) NOT NULL,
    parser_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uniq_parse_result_document_version
ON document_parse_results(document_id, document_version);

CREATE INDEX idx_parse_results_tenant_doc
ON document_parse_results(user_id, knowledge_base_id, document_id);
```

|字段|类型|说明|
|---|---|---|
|content_path|TEXT|标准化文本存储路径，MVP 为本地磁盘相对路径，例如 `parsed/{user_id}/{document_id}/v{document_version}/content.txt`|
|content_hash|VARCHAR|解析后文本 hash，用于排障、重复判断和结果一致性校验|
|metadata|JSONB|页码映射、标题层级、段落位置等结构化信息|
|parser_name / parser_version|VARCHAR|解析器名称和版本，解析策略升级时可追溯|

> `parse` 阶段成功后写入此表；`chunk` 阶段只读取 `document_parse_results`，不直接读取原始上传文件。`storage_path` 和 `content_path` 都保持存储后端无关，业务代码不应依赖本地绝对路径，后续可迁移到对象存储。

MVP 解析器固定为：PDF 使用 PyMuPDF 按页提取文本（不做 OCR）；DOCX 使用 python-docx 提取段落、标题和表格；Markdown 使用 markdown-it-py；TXT 使用 charset-normalizer 探测编码并规范化为 UTF-8。解析包装层不得执行宏、脚本、外部链接或嵌入对象，必须防护路径穿越、压缩炸弹、解压后大小超限和解析超时。标准化文本为空时不写成功解析结果，资料与任务以 `20010 EMPTY_DOCUMENT` 收敛为 `failed`。

---

##### 4. chunks — 分块表（核心向量表）

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE chunks (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id       UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content           TEXT NOT NULL,            -- 原始文本
    content_tokens    INTEGER,                  -- token 数量（估算）
    chunk_index       INTEGER NOT NULL,         -- 在文档中的顺序（从 0 开始）
    embedding         VECTOR(1536) NOT NULL,    -- text-embedding-3-small 维度
    embedding_model   VARCHAR(64) NOT NULL DEFAULT 'text-embedding-3-small',
    embedding_version VARCHAR(32),
    metadata          JSONB DEFAULT '{}',       -- 页码、标题、章节等
    document_version  INTEGER NOT NULL,         -- 对应源文件内容版本
    chunk_strategy_version VARCHAR(32) NOT NULL DEFAULT 'v1-paragraph-512-64',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 向量相似度索引（HNSW，召回效果优于 IVFFlat）
CREATE INDEX idx_chunks_embedding ON chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- 过滤索引
CREATE INDEX idx_chunks_kb_id   ON chunks(knowledge_base_id);
CREATE INDEX idx_chunks_user_id ON chunks(user_id);
CREATE INDEX idx_chunks_tenant_kb_doc_version
ON chunks(user_id, knowledge_base_id, document_id, document_version);

CREATE UNIQUE INDEX uniq_chunks_document_version_strategy
ON chunks(document_id, document_version, chunk_strategy_version, embedding_model, chunk_index);

-- MVP 必备关键词召回索引（无需中文分词扩展）：
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_chunks_trgm ON chunks USING GIN(content gin_trgm_ops);

-- 可选增强迁移：仅当部署环境已安装 pg_jieba 或 zhparser 时执行
-- ALTER TABLE chunks ADD COLUMN content_ts TSVECTOR
--     GENERATED ALWAYS AS (to_tsvector('jieba', content)) STORED;
-- CREATE INDEX idx_chunks_fts ON chunks USING GIN(content_ts);
```

**分块参数（MVP 默认值）：**

|参数|值|说明|
|---|---|---|
|chunk_size|512 tokens|适合大多数中文语料|
|chunk_overlap|64 tokens|保留上下文连接|
|分块策略|段落优先，超长则截断||

**metadata 示例：**

```json
{
  "page": 3,
  "section": "第二章 引言",
  "source_filename": "report.pdf"
}
```

**版本控制约束：**

- `document_version` 对应 `documents.version`，确保每个 chunk 都能追溯到源文档版本。
- `chunk_strategy_version` 标识分块策略，例如 `v1-paragraph-512-64`；调整 chunk size、overlap 或段落规则时必须升级。
- `embedding_model` / `embedding_version` 标识向量模型版本；更换 embedding 模型时不能混用旧向量。
- `embed` 将已生成向量的分块幂等写入正式 `chunks`；在 `finalize` 将资料翻转为 `completed` 前，这些行物理存在但逻辑未发布，任何业务读取都不得返回。草稿分块仍保存在 `document_chunk_drafts`。

---

##### 5. conversations — 对话表

```sql
CREATE TABLE conversations (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    title             VARCHAR(500),
    last_message_at   TIMESTAMPTZ,             -- 每次发送消息后由应用层更新，用于列表排序
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_conv_user_id  ON conversations(user_id);
CREATE INDEX idx_conv_last_msg ON conversations(user_id, last_message_at DESC);
```

|字段|类型|说明|
|---|---|---|
|knowledge_base_id|UUID|必填；MVP 对话必须绑定当前用户有权访问的知识库，知识库删除时级联删除对话|
|title|VARCHAR|首轮消息自动截取或 AI 生成|
|last_message_at|TIMESTAMPTZ|与 updated_at 分离，专用于"最近对话"排序|

---

##### 6. messages — 消息表

```sql
CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            VARCHAR(20) NOT NULL,   -- user | assistant
    status          VARCHAR(20) NOT NULL DEFAULT 'completed',
    -- streaming | completed | failed | cancelled
    content         TEXT NOT NULL,
    rewritten_query TEXT,                   -- 查询改写后的内容（仅 user 消息）
    finish_reason   VARCHAR(32),            -- stop | length | error | cancelled
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_msg_conv_id      ON messages(conversation_id);
CREATE INDEX idx_msg_conv_created ON messages(conversation_id, created_at ASC);
CREATE INDEX idx_msg_user_conv_created ON messages(user_id, conversation_id, created_at ASC);
```

|字段|类型|说明|
|---|---|---|
|status|VARCHAR|消息状态。user 消息直接为 `completed`；assistant 流式生成时先为 `streaming`，正常完成或可信无证据答复为 `completed`，供应商/模型/服务错误重试耗尽为 `failed`，客户端连接断开为 `cancelled`。API 进程失联时，维护扫描器以 `status=streaming AND created_at < now()-MESSAGE_STREAMING_STALE_SECONDS` 条件更新为 `failed`|
|finish_reason|VARCHAR|assistant 结束原因：正常为 `stop` / `length`，失败为 `error`，客户端连接断开为 `cancelled`；与 status 配对且不得遗留 `streaming`。`MESSAGE_STREAMING_STALE_SECONDS` 至少覆盖全部改写、重排和生成最大尝试预算加 60 秒，默认 360 秒|

---

##### 7. message_citations — 引用溯源表

```sql
CREATE TABLE message_citations (
    id          UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID             NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    knowledge_base_id UUID       NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    message_id  UUID             NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chunk_id    UUID             REFERENCES chunks(id) ON DELETE SET NULL,
    document_id UUID             REFERENCES documents(id) ON DELETE SET NULL,
    -- ↑ 冗余存储，避免前端展示引用时的多层 JOIN（chunks → documents）；源数据删除后保留快照
    score       DOUBLE PRECISION NOT NULL, -- 召回融合或重排分数
    rank        INTEGER NOT NULL CHECK (rank >= 1), -- 最终展示顺序（1 = 最相关）
    document_version INTEGER NOT NULL,
    chunk_snapshot JSONB NOT NULL,
    UNIQUE (message_id, rank)
);

CREATE INDEX idx_cite_msg_id ON message_citations(message_id);
CREATE INDEX idx_cite_user_msg ON message_citations(user_id, message_id);
CREATE INDEX idx_cite_user_kb_rank ON message_citations(user_id, knowledge_base_id, rank);
```

> 此表支持“查看引用来源”功能。`user_id` 与 `knowledge_base_id` 是防御性租户边界，查询必须先按当前用户和对话过滤。删除知识库时级联删除其对话、消息和引用；仅删除资料时，`document_id` / `chunk_id` 可置空并通过 `chunk_snapshot` 保留历史来源快照。

---

##### 8. document_tasks — 文档任务表

```sql
CREATE TABLE document_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    document_version INTEGER NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,

    task_type VARCHAR(32) NOT NULL,
    -- parse | chunk | embed | finalize | cleanup | delete_cleanup

    delete_cycle INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT ck_document_tasks_delete_cycle CHECK (
        (task_type = 'delete_cleanup' AND delete_cycle > 0)
        OR (task_type <> 'delete_cleanup' AND delete_cycle = 0)
    ),

    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- pending | queued | running | succeeded | failed | cancelled

    idempotency_key VARCHAR(128) NOT NULL,
    task_payload JSONB NOT NULL DEFAULT '{}',
    task_result JSONB,
    error_code INTEGER,
    error_message TEXT,
    total_items INTEGER,
    processed_items INTEGER NOT NULL DEFAULT 0,
    checkpoint JSONB NOT NULL DEFAULT '{}',

    depends_on_task_id UUID REFERENCES document_tasks(id) ON DELETE SET NULL,

    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,

    queued_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 供 document_task_attempts 的复合外键引用，强制 attempt 的边界与父任务一致。
    CONSTRAINT uq_document_tasks_attempt_scope
        UNIQUE (id, user_id, knowledge_base_id, document_id, document_version)
);

CREATE UNIQUE INDEX uniq_document_task_idempotency
ON document_tasks(idempotency_key);

CREATE INDEX idx_document_tasks_document_id
ON document_tasks(document_id);

CREATE INDEX idx_document_tasks_status
ON document_tasks(status);

CREATE INDEX idx_document_tasks_doc_type
ON document_tasks(document_id, task_type);

CREATE INDEX idx_document_tasks_tenant_status
ON document_tasks(user_id, knowledge_base_id, status);
```

|字段|类型|说明|
|---|---|---|
|document_version|INTEGER|任务绑定的文档版本，防止旧版本任务污染新版本 chunks|
|task_type|VARCHAR|处理阶段：`parse` / `chunk` / `embed` / `finalize` / `cleanup` / `delete_cleanup`；前者只清理旧版本，后者只清理删除资料|
|status|VARCHAR|任务状态：`pending` / `queued` / `running` / `succeeded` / `failed` / `cancelled`|
|delete_cycle|INTEGER|删除清理轮次；仅 `delete_cleanup` 使用且必须大于 0，其他任务为 0；用于保留每轮删除的独立审计记录|
|idempotency_key|VARCHAR|幂等键；普通阶段格式为 `{task_type}:{document_id}:v{document_version}`，删除清理为 `delete_cleanup:{document_id}:v{document_version}:d{delete_cycle}`，因此重试删除会创建新任务而不覆盖历史|
|error_code / error_message|INTEGER / TEXT|任务异步失败业务码与安全摘要；任务详情接口仍返回 HTTP 200|
|total_items / processed_items|INTEGER|任务处理总量和已完成数量，例如 embed 已写入 chunk 数|
|checkpoint|JSONB|任务恢复点，例如当前批次、最后处理的 `chunk_index`、外部任务 ID|
|depends_on_task_id|UUID|上游任务依赖，用于表达 parse → chunk → embed → finalize|

> `retry_count` 表示首次执行之外已经启动的重试次数；初次 attempt 的 `attempt_no=1` 且
> `retry_count=0`，每次重试先递增 `retry_count` 再创建下一个 attempt。MVP 的 `max_retries=3`
> 表示初次执行后最多再重试 3 次，因此单个任务最多创建 4 个 attempt（`attempt_no=1..4`）；当
> `retry_count >= max_retries` 时不得再次排队。该语义对每个独立任务成立，包括每一轮新建的
> `delete_cleanup`，新删除轮次从 0 重新计数，且不得修改旧轮次记录。
>
> `documents.status` 用于前端展示，`document_tasks.status` 用于系统内部任务编排。Celery 重投递时必须先按 `idempotency_key` 查重。

##### 8.1 document_upload_requests — 上传请求幂等表

```sql
CREATE TABLE document_upload_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(128) NOT NULL,
    request_fingerprint VARCHAR(128) NOT NULL,
    upload_batch_id UUID NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'coordinating', -- coordinating | accepted | failed
    response_snapshot JSONB,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, knowledge_base_id, idempotency_key)
);
```

`request_fingerprint` 只用于识别“同键不同请求”，`upload_batch_id` 和 `status` 用于区分仍在协调与
已经收敛的首次请求，`response_snapshot` 只保存资料 ID 与必要状态，不保存文件正文。文件全部转正
或补偿失败时，必须与资料和初始任务在同一事务更新为 `queued` 或 `failed/20011`。同键命中未超时
`coordinating` 时返回 `20008/409` 且无副作用；超过 `DOCUMENT_UPLOAD_PENDING_TIMEOUT_SECONDS`
（默认 300 秒）后，重放请求可获取批次锁并调用与恢复扫描器相同的幂等协调函数接管。默认保留
24 小时，过期后由现有 Celery Beat 恢复/维护扫描器批量删除。

##### 8.2 document_processing_leases — 用户处理并发名额表

```sql
CREATE TABLE document_processing_leases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    task_id UUID REFERENCES document_tasks(id) ON DELETE SET NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ,
    release_reason VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uniq_active_document_processing_lease
ON document_processing_leases(document_id)
WHERE released_at IS NULL;

CREATE INDEX idx_processing_leases_user_active
ON document_processing_leases(user_id, expires_at)
WHERE released_at IS NULL;
```

获取名额时必须在数据库事务内对用户维度串行化并统计未释放记录，不能用“先 count 后 insert”的
无锁流程。名额归属于资料的整条处理流水线，从资料首次进入 `processing` 起跨 parse、chunk、embed、
finalize 持续持有；`task_id` 只表示当前阶段归属，阶段切换时更新，不得释放后重新竞争。终态、取消和
删除时若无活动 attempt 则主动释放；若仍有活动 attempt，删除事务锁定 lease 并以当时的
`expires_at` 冻结等待上限，资料进入 `deleting` 后心跳不得续租；恢复扫描器在超时后事务性取消
attempt/task 并释放名额。若 running attempt 没有活动 lease，则立即按失联接管。

---

##### 9. document_task_attempts — 文档任务尝试记录表

```sql
CREATE TABLE document_task_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    document_version INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    worker_name VARCHAR(128),
    status VARCHAR(20) NOT NULL,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- task_id 与冗余边界必须引用同一条父任务，不能只依赖各列的独立外键。
    CONSTRAINT fk_document_task_attempt_parent_scope
        FOREIGN KEY (task_id, user_id, knowledge_base_id, document_id, document_version)
        REFERENCES document_tasks (id, user_id, knowledge_base_id, document_id, document_version)
        ON DELETE CASCADE
);

CREATE INDEX idx_task_attempts_task_id
ON document_task_attempts(task_id);

CREATE INDEX idx_task_attempts_tenant_document
ON document_task_attempts(user_id, knowledge_base_id, document_id, started_at DESC);

CREATE UNIQUE INDEX uniq_task_attempt_no
ON document_task_attempts(task_id, attempt_no);

CREATE UNIQUE INDEX uniq_active_task_attempt
ON document_task_attempts(task_id)
WHERE finished_at IS NULL;
```

> 此表是排障视角状态：记录每次任务尝试、失败原因、worker 信息和耗时。父任务对 `(id, user_id, knowledge_base_id, document_id, document_version)` 建立复合唯一键，attempt 以 `(task_id, user_id, knowledge_base_id, document_id, document_version)` 建立复合外键；因此 `user_id`、`knowledge_base_id`、`document_id` 和 `document_version` 必须在数据库层与 `task_id` 指向的同一父任务完全一致，而不是仅靠各列的独立外键或应用约定。创建 attempt 时仍必须在同一事务锁定父任务并复制、校验四者一致。任务每次开始执行都必须创建 `running` attempt，结束或失联恢复时必须写入 `finished_at` 和明确状态；同一任务不得同时存在两个未结束 attempt。attempt 详情与诊断读取必须经带当前 `user_id` 条件的仓储方法完成，不得直接按 `task_id` 查询。

---

##### 10. document_chunk_drafts — 分块草稿表

```sql
CREATE TABLE document_chunk_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    document_version INTEGER NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    knowledge_base_id UUID NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_tokens INTEGER,
    metadata JSONB DEFAULT '{}',
    chunk_strategy_version VARCHAR(32) NOT NULL DEFAULT 'v1-paragraph-512-64',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uniq_chunk_drafts_document_version_strategy
ON document_chunk_drafts(document_id, document_version, chunk_strategy_version, chunk_index);

CREATE INDEX idx_chunk_drafts_tenant_doc
ON document_chunk_drafts(user_id, knowledge_base_id, document_id);
```

> `document_chunk_drafts` 保存 chunk 阶段的中间结果。`embed` 仅在向量生成成功后才把对应分块写入 `chunks`，所以表内不存在 `embedding IS NULL` 的行；是否可检索由关联资料的 `completed` 状态和当前版本共同决定。

---

### 关键实现约束

#### 文档处理状态机

| # | 当前状态 | 触发事件 | 目标状态 | 约束 |
|---|---|---|---|---|
| 1 | — | 创建文档 | `pending` | 同一事务创建不可执行的初始 `parse` 任务。 |
| 2 | `pending` | 上传协调、投递成功 | `queued` | 任务成为可执行状态；Celery 不是状态真相源。 |
| 3 | `queued` | worker 认领 | `processing` | 取得资料级处理名额并创建 running attempt。 |
| 4 | `processing` | `finalize` 成功 | `completed` | 校验当前版本正式 chunks 后才可发布。 |
| 5 | `processing` | `parse` / `chunk` / `embed` / `finalize` 当前子阶段重试耗尽 | `failed` | `current_task_type` 记录失败子阶段；不得经过 `completed`。 |
| 6 | `pending` / `queued` / `processing` / `completed` / `failed`（非 `delete_cleanup`） | 用户发起 DELETE | `deleting` | 与流水线阶段无关；取消未开始任务，并等待运行 attempt 自然停止或经 fencing/孤儿扫描安全接管后才激活删除清理。 |
| 7 | `deleting` | `delete_cleanup` 成功 | `deleted` | 原始文件和派生数据清理完毕；历史引用快照不受影响。 |
| 8 | `deleting` | `delete_cleanup` 重试耗尽 | `failed` | `current_task_type = delete_cleanup`，错误码 `20015`；列表/详情仅向所属用户显示“删除未完成”最小墓碑和“重试删除”，检索仍不可见。 |
| 9 | `failed`（`current_task_type = delete_cleanup`） | 再次发起 DELETE | `deleting` | 递增 `delete_cycle` 并新建 `delete_cleanup` 任务/attempt；历史任务、attempt 与重试计数不可变，不触发 `parse` / `chunk` / `embed` / `finalize`。 |

- 资料 DELETE 使用独立、带 `user_id` 的锁定变更查询，可命中普通可见资料、`deleting` 与
  `failed/delete_cleanup/20015`，不得复用于 GET/list。命中 `deleting` 时幂等返回成功，不递增
  `delete_cycle`、不创建清理任务；命中 `deleted` 时返回 404。
- 上传成功后先创建 `documents`，再创建 `parse` 任务。
- 进入任务队列时更新 `documents.status = queued`。
- 文档创建或排队时可预填 `current_task_type = parse`；任一任务运行时更新 `documents.status = processing` 和对应的 `current_task_type`。
- `finalize` 成功后统一回写 `completed`、`chunk_count`、`processing_finished_at`。
- `cleanup` 在 `finalize` 之后异步触发，只清理旧版本派生数据；检索正确性不依赖 cleanup。
- 删除文档时进入 `deleting`，取消未开始任务并从列表、详情与检索隐藏；专用 `delete_cleanup`
  清理文件和全部派生数据后保留最小墓碑并进入 `deleted`。若清理重试耗尽，则进入
  `failed/delete_cleanup/20015`，只暴露最小“删除未完成”墓碑和重试删除入口；重试新建清理任务而不重置历史。MVP 不直接物理删除资料行。

#### 事务边界

- `parse`：提交解析结果，不写正式 `chunks`。
- `chunk`：写 `document_chunk_drafts`，不写 embedding。
- `embed`：按批次写正式 `chunks`，例如每 50 / 100 个 chunk 一个事务。
- `finalize`：单独事务回写 `documents`。
- `cleanup`：删除或归档旧版本 `chunks`、`document_chunk_drafts`、`document_parse_results`。
- `delete_cleanup`：只在资料删除编排取得安全接管权后，删除原始对象与该资料全部派生数据。

Embedding 调用不应包在长事务里，避免外部模型延迟导致数据库连接被长时间占用。

#### 上传与任务创建一致性

对象存储和数据库不能放在同一个强事务里，MVP 使用补偿式一致性：

1. 整批同步预校验通过后生成内部 `upload_batch_id`，将本批所有文件写入可由批次和资料 ID 推导的临时对象路径，例如 `tmp/{user_id}/{upload_batch_id}/{document_id}`。
2. 在一个数据库事务内创建本批全部 `pending` 状态的 `documents`、不可执行的 `pending` 初始 `parse` 任务和可选幂等记录；资料保存相同 `upload_batch_id`。
3. 如果数据库事务失败，回滚记录、删除本批全部临时对象并返回 `50000/500`。
4. 事务提交成功后，协调器按 `upload_batch_id` 对整批 documents 执行
   `SELECT ... FOR UPDATE SKIP LOCKED`，在持有该短事务行锁期间逐个执行同一本地卷的原子重命名，
   并更新本批 `documents.updated_at`。重放或扫描器获取不到锁时跳过；进程崩溃后行锁自动释放。
5. 任一对象转正失败时，清理本批全部临时对象和已经转正的对象，并在一个补偿事务中把本批资料、初始任务及幂等响应快照全部更新为 `failed/20011`；不得投递 `parse`，但仍以 `202` 返回这些已持久化的失败资料。
6. 全部对象转正成功时，在一个数据库事务中把本批资料、初始任务及幂等响应快照切换为 `queued`，随后投递初始任务并返回 `202`。这里的“整批”是补偿式一致性，不宣称文件系统与数据库具有分布式强事务。

MVP 默认把 `/data/orionamesh` 挂载为本地持久卷，数据库只保存相对对象键。批量上传可携带可选
`Idempotency-Key`；同一用户、同一知识库和同一键命中已收敛结果时返回首次 `202` 结果，不重复
创建资料、任务或对象；命中未超过 300 秒的 `coordinating` 请求时返回 `20008/409` 且无副作用，
超时后重放请求可按批次锁接管；同键不同请求返回冲突。不提供该键时，即使内容哈希相同也创建独立资料。

任务调度以数据库为真相源：Celery 只负责执行任务，不能作为唯一状态来源。初始 parse 任务与资料
同时为 `pending` 时表示文件转正尚未完成，worker 不得执行。恢复扫描器必须先按 `upload_batch_id`
锁定超过 `DOCUMENT_UPLOAD_PENDING_TIMEOUT_SECONDS`（默认 300 秒）且复查仍超时的批次：批内每份
资料已有正式对象或仍有完整临时对象时，幂等完成剩余转正并原子切换资料、任务及幂等响应快照为
`queued`；任一资料的正式与临时对象都缺失、损坏或转正失败时整批补偿为 `failed/20011`。普通
任务扫描只幂等投递 `queued`。对于处理名额已过期且任务仍为 `running` 的记录，扫描器必须在同一
数据库事务中锁定任务、attempt、资料和名额：正常资料按重试预算恢复或失败；`deleting` 资料则将
attempt/task 置为 `cancelled`、释放名额并激活 `delete_cleanup`。同一任务不得同时存在两个未结束 attempt。

#### 阶段编排与写入 fencing

`parse → chunk → embed → finalize` 的成功切换必须由统一编排器完成。编排事务锁定当前 attempt、
task、document 和 processing lease，验证 attempt/task 仍为 `running`、资料版本一致且未进入
`deleting/deleted`；随后完成当前 attempt/task、按幂等键创建或激活下一任务、更新
`documents.current_task_type` 与 `lease.task_id` 并提交。只有提交后才投递 Celery；提交成功但投递
失败的 `queued` 任务由恢复扫描器重投。`finalize` 成功在同一事务翻转资料完成状态并释放名额。

所有解析结果、草稿、正式 `chunks`、checkpoint 和阶段结果引用的仓储写方法都必须接收当前
`attempt_id` 作为 fencing token。每次写入前在同一数据库事务中锁定并校验 attempt/task 为
`running`、资料版本一致且 document 非 `deleting/deleted`，校验失败则整笔写入不发生并使 worker
收敛为取消。外部模型调用不包含在该事务中，避免长事务。

同一个 Celery Beat 恢复/维护扫描器还必须按小批量删除 `expires_at < now()` 的 `document_upload_requests`，避免 24 小时幂等记录无限增长；不得为此新增独立服务或记录幂等键原值日志。

#### embedding 幂等入库

`embed` 任务必须按 `document_id + document_version + chunk_strategy_version + embedding_model + chunk_index` 幂等写入正式 `chunks`。

Embedding 在 30 秒超时和 2 次指数退避重试后仍失败时，资料与任务持久化
`20012 DOCUMENT_EMBEDDING_FAILED`；不得把供应商原始响应或资料内容写入错误摘要。

推荐写入方式：

```sql
INSERT INTO chunks (
  document_id,
  knowledge_base_id,
  user_id,
  content,
  content_tokens,
  chunk_index,
  embedding,
  embedding_model,
  embedding_version,
  metadata,
  document_version,
  chunk_strategy_version
)
VALUES (...)
ON CONFLICT (
  document_id,
  document_version,
  chunk_strategy_version,
  embedding_model,
  chunk_index
)
DO UPDATE SET
  content = EXCLUDED.content,
  content_tokens = EXCLUDED.content_tokens,
  embedding = EXCLUDED.embedding,
  embedding_version = EXCLUDED.embedding_version,
  metadata = EXCLUDED.metadata;
```

重试策略：

- 批次成功后更新 `document_tasks.processed_items` 和 `checkpoint`。
- 任务重试时从 `checkpoint` 继续；即使重复处理已成功批次，也依赖 `ON CONFLICT` 保证不产生重复 chunks。
- `finalize` 前必须校验正式 `chunks` 数量等于 `document_chunk_drafts` 数量；不一致则任务失败，不允许把文档标记为 `completed`。

#### chunks 统一读取边界

所有 `chunks` 读取必须通过统一 `ChunkRepository`，路由、业务服务和 worker 不得直接执行该表的查询：

- 面向检索和引用活表的读取必须 `JOIN documents`，并强制过滤当前认证用户、当前知识库、`d.status = 'completed'` 以及 `c.document_version = d.version`。
- 面向 finalize/cleanup 的内部计数或校验必须强制过滤当前用户、知识库、资料 ID 和精确 `document_version`；内部方法不得被面向用户的查询复用。
- 架构测试必须扫描直接 SQL/ORM 访问与依赖导入，确保除迁移、仓储实现和测试夹具外没有读取旁路。

#### cleanup 任务

`cleanup` 任务在 `finalize` 成功后创建，目标是回收旧版本派生数据，不负责保证检索正确性。

可清理范围：

- 同一 `document_id` 下 `document_version < documents.version` 的 `chunks`
- 同一 `document_id` 下旧版本 `document_chunk_drafts`
- 同一 `document_id` 下旧版本 `document_parse_results`

保留策略：

- `message_citations` 不删除，历史回答依赖 `chunk_snapshot` 展示当时引用内容。
- cleanup 失败只记录任务失败，不回滚 `documents.status = completed`。
- 所有 retrieval SQL 必须通过 `c.document_version = d.version` 过滤当前版本，因此旧版本 chunks 即使暂未清理，也不会被召回。

#### delete_cleanup 与知识库删除

资料 DELETE 命令使用独立、带 `user_id` 的锁定变更查询。首次删除才标记 `deleting`、取消未开始任务、
递增 `delete_cycle` 并创建独立 `delete_cleanup`；命中 `deleting` 时幂等成功且不递增轮次、不创建任务；
命中 `failed/delete_cleanup/20015` 时递增轮次并新建任务；命中 `deleted` 时返回 404。无活动 attempt 时
立即释放名额并激活清理；存在活动 attempt 时不提前释放 lease，并以删除事务锁定时的
`expires_at` 冻结等待上限，后续心跳不得续租。running attempt 没有活动 lease 时立即接管。
worker 的下一次写入会被 fencing 拦截并主动取消；worker 卡死则由上述孤儿扫描在超时后强制接管。若
`delete_cleanup` 重试耗尽，资料进入 `failed`、记录 `current_task_type = delete_cleanup` 和 `20015`；资料所属用户
只能看到最小删除失败墓碑并再次 DELETE。从该失败状态再次 DELETE 只创建下一轮清理任务，旧任务、attempt 和 retry_count 均不可修改。

知识库 DELETE 先将 `knowledge_bases.status` 标为 `deleting` 并对全部资料执行相同编排，从提交起
隐藏知识库及子资源。DELETE 命令使用独立、带 `user_id` 的变更查询，可命中所属用户的
`active/deleting/delete_failed` 知识库；该查询不得复用于普通 GET/list、对话或其他子资源路由。命中
`deleting` 时幂等成功且不创建任务。若任一子资料的 `delete_cleanup` 重试耗尽，维护扫描器将知识库
收敛为 `delete_failed`、`delete_error_code=20015`，仅向所属用户返回不含名称、描述和子资源的最小
“删除未完成”墓碑，`allowed_actions` 仅为 `retry_delete`。所属用户再次 DELETE 时才转回 `deleting`，
并仅为 `failed/delete_cleanup` 子资料创建新的删除轮次。全部资料为
`deleted` 且无活动 attempt 后，维护扫描器才物理删除知识库并级联对话、消息和引用；空知识库可在删除
事务内直接删除，物理删除后再次 DELETE 返回 404。不得用立即数据库级联代替文件清理。

#### 版本控制底线

以下字段是 MVP 的最低版本控制集合：

- `documents.version`
- `document_tasks.document_version`
- `document_chunk_drafts.document_version`
- `chunks.document_version`
- `message_citations.document_version`

文档版本递增发生在：用户重新上传覆盖源文件、编辑文档内容后重建、或者主动更换分块策略并重建该文档。

#### 文档重处理边界

MVP 阶段不开放文档重新处理 API。`documents.version`、`document_version`、`cleanup` 和相关唯一约束先作为数据一致性骨架保留，保证后续扩展时不需要推翻表结构。

Phase 2 可开放以下端点：

```http
POST /knowledge-bases/{kb_id}/documents/{doc_id}/reprocess
```

用途：基于当前 `documents.storage_path` 重新执行 `parse -> chunk -> embed -> finalize -> cleanup`，并在创建新流水线前递增 `documents.version`。

如需替换源文件，使用独立端点：

```http
PUT /knowledge-bases/{kb_id}/documents/{doc_id}/file
```

用途：上传新的源文件，更新 `storage_path`，递增 `documents.version`，然后触发完整重处理流水线。

#### 资料处理并发控制

- 单用户同时处于 `processing` 的文档默认最多 3 个，超过后任务保持 `queued`。
- 并发名额必须在数据库事务中按用户原子获取并与资料/任务绑定；完成、失败、取消或失联恢复时释放。删除时仅无活动 attempt 的资料可立即释放；有活动 attempt 时必须冻结当时的 lease `expires_at`、禁止续租，并在 worker 正常取消或恢复扫描器安全接管后才释放。Redis 与 Celery 均不是名额真相源。
- 单文档同一 `document_version` 同一 `task_type` 只能存在一个未终态任务，由 `idempotency_key` 保证；
  `delete_cleanup` 以 `delete_cycle` 区分每次有效新删除轮次（首次删除或删除清理失败后的重试），
  `deleting` 状态的幂等重放不产生新轮次；历史终态清理任务可保留，下一轮不会覆盖它。
- embedding 阶段按批次处理，单批建议 50 / 100 个 chunk，失败只重试当前任务，不回滚已成功的其他文档。
- 用户删除文档时，后续未开始任务标记为 `cancelled`；运行中任务除阶段边界检查外，还必须由每次
  持久化写入的数据库 fencing 强制阻止删除后的写入，失联等待不得超过活动 lease 的 `expires_at`。

#### 降级策略

模型供应商、端点、共享凭证及四类模型名均由 `MODEL_GATEWAY_*` 环境变量配置。MVP 的
`MODEL_GATEWAY_PROVIDER` 固定支持 `openai-compatible` 协议适配器；`MODEL_GATEWAY_ENDPOINT` 无默认值，
必须显式配置并使用合法 HTTPS base URL。仅本地开发和自动化测试允许使用 HTTP，且主机名必须精确为
`localhost`，或地址必须为回环 IP `127.0.0.1`/`::1`；其他 HTTP endpoint 一律拒绝就绪，无需引入环境模式变量。
适配器通过 endpoint、API key 和模型名
连接兼容供应商；Embedding 使用兼容 embeddings 端点，Query Rewrite/Generation 使用兼容 chat 端点；
可选 Reranker 配置后通过同一 chat 端点返回 `{"scores":[{"candidate_index":0,"score":0.0}]}`；每个候选
临时序号必须恰好出现一次，序号不得重复或越界，score 必须为有限数值。合法结果按 score 降序且同分
保持原 RRF 顺序；响应解析或校验失败时整体回退 RRF，不应用部分评分。未配置或调用失败时同样直接使用
RRF。未知 provider
必须在启动就绪校验中失败，不得静默回退。Embedding 默认
`text-embedding-3-small`；Query Rewrite 与 Generation 模型必须显式配置；Reranker 模型为空时
禁用重排且不影响就绪。模型网关是超时与重试的唯一执行者，集中完成路由、凭证注入、脱敏、稳定错误分类和
白名单审计，只向业务用例适配器返回最终成功或失败；适配器不得自行再次超时或重试。Embedding 失败由嵌入用例
收敛资料/任务失败，改写失败使用原问题，Reranker 失败使用 RRF，生成失败收敛 `failed/error`。Embedding/改写/Reranker/生成分别使用 30 秒/2 次、10 秒/1 次、
10 秒/1 次、首 token 15 秒且总时长 120 秒/1 次的默认超时与重试，变量名称以
`specs/001-orionamesh-rag-mvp/quickstart.md` 为准。

- Reranker 不可用：跳过 rerank，直接使用 RRF 融合结果进入 Context Pack。
- 中文 FTS 扩展不可用：MVP 必须退化为 `pg_trgm` 关键词召回；如果 `pg_trgm` 也不可用，则部署检查失败，不进入可用状态。
- Query Rewrite 失败：回退到用户原始问题继续检索。
- SSE 客户端连接断开：已保存的 user message 保留；已创建但未完成的 assistant message 必须持久化为 `status = cancelled`、`finish_reason = cancelled`。
- 供应商、模型或服务错误：按既定重试耗尽后 assistant message 必须持久化为 `status = failed`、`finish_reason = error` 并发送 `error` 事件；正常完成和可信无证据答复为 `completed/stop`。API 进程崩溃或终态写入中断后，复用 Celery Beat 维护扫描器将超时且仍为 `streaming` 的消息条件收敛为 `failed/error`。所有分支均不得删除已创建消息或遗留 `streaming`。

#### MVP 实现阶段

1. **Phase 1：文档入库闭环**
   - 用户注册 / 登录
   - 知识库 CRUD
   - 文档上传、对象存储、异步任务表
   - parse / chunk / embed / finalize / cleanup
   - pgvector 向量召回与 pg_trgm 关键词召回
   - RRF 融合、Context Pack 与引用来源落库

2. **Phase 2：检索质量增强**
   - 文档 reprocess API
   - 文档源文件替换 API
   - 可选中文 FTS 召回
   - Reranker 默认启用、模型选择与质量调优（MVP 已提供可选适配器和 RRF 降级）

3. **Phase 3：对话体验**
   - 会话 CRUD
   - 历史消息分页
   - SSE 流式回答
   - 引用来源按需查看
   - 断开连接取消生成

#### API 接口文档

**Base URL：** `https://api.orionamesh.com/v1`

**认证：** 所有接口（除 `POST /users`、`POST /auth/sessions` 外）需携带 Header：

```
Authorization: Bearer <access_token>
```

**统一响应格式：**

```json
{
  "code": 0,
  "data": {},
  "msg": "string",
  "trace_id": "uuid"
}
```

`code = 0` 表示成功，非 0 使用稳定业务错误码；删除成功同样返回此信封且 `data = null`。
为聚焦业务字段，下方部分响应示例省略 `code`、`msg` 和 `trace_id`；实际响应不得省略这些信封字段。

---

##### 模块一：认证与用户

##### `POST /users` — 注册

> 注册只创建用户资源，Token 需调用登录接口获取，两步分离语义清晰。

**Request Body：**

```json
{
  "email": "user@example.com",
  "password": "Str0ngP@ss",
  "display_name": "张三"
}
```

**Response 201：**

```json
{
  "data": {
    "id": "uuid",
    "email": "user@example.com",
    "display_name": "张三",
    "created_at": "2025-01-01T00:00:00Z"
  }
}
```

**错误码：**

- `400 INVALID_REQUEST` — 参数校验失败（密码强度不足等）
- `409 EMAIL_ALREADY_EXISTS` — 邮箱已注册

---

##### `POST /auth/sessions` — 登录

> 登录本质是"创建一个 session"，用资源名词而非动词。
>
> 登录成功后创建 `auth_sessions` 记录，并更新 `users.last_login_at = now()`。refresh token 明文只返回给客户端一次，服务端只保存哈希。

**Request Body：**

```json
{
  "email": "user@example.com",
  "password": "Str0ngP@ss"
}
```

**Response 201：**

```json
{
  "data": {
    "access_token": "eyJ...",
    "refresh_token": "rt_7f6f1c9b0d8e4a2b...",
    "expires_in": 7200,
    "user": {
      "id": "uuid",
      "email": "user@example.com",
      "display_name": "张三"
    }
  }
}
```

**错误码：**

- `401 INVALID_CREDENTIALS` — 邮箱或密码错误

---

##### `PUT /auth/sessions` — 刷新 Token

> 使用 `PUT` 表示替换当前 session，旧 refresh_token 同时失效。
>
> 刷新成功时必须在同一事务中按 refresh token 摘要锁定旧 session；锁定后仍有效才可将旧 session 写入
> `revoked_at` 并创建通过 `rotated_from_session_id` 指向它的新 session。同一 token 的并发后到请求取得锁后
> 必须返回 `10006/401`，不得创建第二个后继 session。

**Request Body：**

```json
{
  "refresh_token": "rt_7f6f1c9b0d8e4a2b..."
}
```

**Response 200：**

```json
{
  "data": {
    "access_token": "eyJ...",
    "refresh_token": "rt_2a8d3e6f9b1c4d7e...",
    "expires_in": 7200
  }
}
```

**错误码：**

- `401 INVALID_REFRESH_TOKEN` — refresh_token 无效、已过期、已撤销或发生重放；客户端必须重新登录

---

##### `DELETE /auth/sessions` — 登出

> 请求必须同时提供 `Authorization: Bearer <access_token>` 与以下请求体。服务端对 refresh token
> 做哈希，验证对应会话属于当前认证用户后写入 `auth_sessions.revoked_at`；重复撤销保持幂等。
> Redis 只作为可选加速层，不得使用黑名单或直接删除作为唯一失效依据。

**Request Body：**

```json
{
  "refresh_token": "rt_2a8d3e6f9b1c4d7e..."
}
```

**Response 200：** 返回统一成功信封，`data` 为 `null`

**错误码：**

- `10001/401 TOKEN_INVALID_OR_EXPIRED` — Access Token 缺失、格式错误、签名或算法无效、必填声明/type 错误或过期
- `401 INVALID_REFRESH_TOKEN` — refresh token 无效、过期、已撤销或不属于当前用户

---

##### 忘记密码 / 密码重置

MVP 不提供任何忘记密码、自助或管理员密码重置、邮件/验证码重置及账号恢复流程；后续版本如需支持，必须另行定义安全流程与接口。

---

##### `GET /users/me` — 获取当前用户信息

**Response 200：**

```json
{
  "data": {
    "id": "uuid",
    "email": "user@example.com",
    "display_name": "张三",
    "created_at": "2025-01-01T00:00:00Z"
  }
}
```

---

##### `PATCH /users/me` — 更新当前用户信息

**Request Body（字段均可选）：**

```json
{
  "display_name": "李四"
}
```

**Response 200：** 返回更新后的用户对象

---

#### 模块二：知识库

##### `GET /knowledge-bases` — 获取知识库列表

**Query Params：**

|参数|类型|说明|
|---|---|---|
|page|int|页码，默认 1|
|page_size|int|每页数量，默认 20，最大 100|

**Response 200：**

```json
{
  "data": {
    "items": [
      {
        "id": "uuid",
        "name": "产品手册",
        "description": "Q3 版本",
        "status": "active",
        "delete_error_code": null,
        "allowed_actions": ["delete"],
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-02T00:00:00Z"
      },
      {
        "id": "uuid",
        "name": null,
        "description": null,
        "status": "delete_failed",
        "delete_error_code": 20015,
        "allowed_actions": ["retry_delete"],
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-02T00:00:00Z"
      }
    ],
    "total": 3,
    "page": 1,
    "page_size": 20
  }
}
```

---

##### `POST /knowledge-bases` — 创建知识库

**Request Body：**

```json
{
  "name": "产品手册",
  "description": "Q3 版本相关资料"
}
```

**Response 201：**

```json
{
  "data": {
    "id": "uuid",
    "name": "产品手册",
    "description": "Q3 版本相关资料",
    "status": "active",
    "delete_error_code": null,
    "allowed_actions": ["delete"],
    "created_at": "2025-01-01T00:00:00Z"
  }
}
```

---

##### `GET /knowledge-bases/{kb_id}` — 获取知识库详情

**Response 200：** 返回单个知识库对象（含完整字段）

**错误码：**

- `404 NOT_FOUND`

---

##### `PATCH /knowledge-bases/{kb_id}` — 更新知识库信息

**Request Body（字段均可选）：**

```json
{
  "name": "新名称",
  "description": "新描述"
}
```

**Response 200：** 返回更新后的知识库对象

**错误码：**

- `404 NOT_FOUND`

---

##### `DELETE /knowledge-bases/{kb_id}` — 删除知识库

> 先将知识库标记为内部 `deleting` 并编排全部资料的有界停止与 `delete_cleanup`。从提交起知识库
> 及子资源立即不可见；所有资料清理完成且无活动 attempt 后才物理删除知识库，并级联绑定对话、
> 消息与引用。DELETE 使用所属用户范围的独立变更查询，可命中 `active/deleting/delete_failed`；普通
> 知识库列表/详情可按所属用户返回 `active` 完整对象或 `delete_failed` 最小墓碑，内容与子资源读取只允许
> `active`。命中 `deleting` 时
> 幂等成功且不创建任务；任一子资料以 `20015` 删除失败后，知识库收敛为 `delete_failed/20015` 最小墓碑，
> `allowed_actions` 仅为 `retry_delete`。再次 DELETE 才转回 `deleting` 并仅为失败子资料创建新删除轮次。
> 物理删除后再次 DELETE 返回 404。禁止以立即数据库级联跳过运行 worker 和持久卷文件清理。

**Response 200：** 返回统一成功信封，`data` 为 `null`

**错误码：**

- `404 NOT_FOUND`

---

#### 模块三：文档

##### `POST /knowledge-bases/{kb_id}/documents` — 上传文档

**Content-Type：** `multipart/form-data`

**可选请求头：** `Idempotency-Key`，用于同一用户和知识库范围内的网络重试防重放，不用于内容去重。

|字段|类型|是否必须|说明|
|---|---|---|---|
|files[]|File[]|是|1～20 个文件；支持 PDF / DOCX / MD / TXT，保留各自原始文件名|

服务端必须先完成整批同步预校验；任一文件格式不支持、单文件超过 50MB 或总数超过 20 时，整批拒绝且不得创建任何资料、任务、幂等结果或正式文件对象。

**Response 202：**（异步处理；每个返回项只能为 `queued`，或同步补偿后的 `failed/20011`；内部
`pending` 不作为本次正常返回状态）

```json
{
  "data": {
    "documents": [
      {
        "id": "uuid",
        "knowledge_base_id": "uuid",
        "filename": "report.pdf",
        "file_type": "pdf",
        "file_size": 204800,
        "status": "queued",
        "version": 1,
        "current_task_type": "parse",
        "retry_count": 0,
        "delete_cycle": 0,
        "chunk_count": 0,
        "error_code": null,
        "error_message": null,
        "processing_started_at": null,
        "processing_finished_at": null,
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
        "allowed_actions": ["delete"]
      }
    ]
  }
}
```

**错误码：**

- `400 UNSUPPORTED_FILE_TYPE`（`20009`）— 仅支持 PDF、DOCX、MD 和 TXT 文件
- `400 FILE_TOO_LARGE` — 文件超过大小限制（MVP 默认 50MB）
- `400 TOO_MANY_FILES` — 单次上传超过 20 个文件
- `404 NOT_FOUND` — 知识库不存在
- `409 RESOURCE_CONFLICT`（`20008`）— 同一 `Idempotency-Key` 的首次上传仍在 300 秒协调窗口内，客户端稍后重试

---

##### `GET /knowledge-bases/{kb_id}/documents` — 获取文档列表

**Query Params：**

|参数|类型|说明|
|---|---|---|
|page|int|页码，默认 1|
|page_size|int|每页数量，默认 20，最大 100|
|status|string|可选，仅允许 `pending` / `queued` / `processing` / `completed` / `failed`；`deleting` / `deleted` 返回 `10003/400`|

> 列表查询必须先固定排除内部 `deleting/deleted`，再应用 `status` 过滤；客户端不能通过过滤参数读取隐藏资料。

**Response 200：**

```json
{
  "data": {
    "items": [
      {
        "id": "uuid",
        "filename": "report.pdf",
        "file_type": "pdf",
        "file_size": 204800,
        "status": "completed",
        "version": 1,
        "current_task_type": null,
        "retry_count": 0,
        "delete_cycle": 0,
        "chunk_count": 43,
        "error_code": null,
        "error_message": null,
        "processing_started_at": "2025-01-01T00:00:02Z",
        "processing_finished_at": "2025-01-01T00:01:00Z",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:01:00Z",
        "allowed_actions": ["delete"]
      }
    ],
    "total": 12,
    "page": 1,
    "page_size": 20
  }
}
```

---

##### `GET /knowledge-bases/{kb_id}/documents/{doc_id}` — 获取文档详情

> 同时用于轮询文档处理状态，前端上传后轮询此接口直到 `status` 为 `completed` 或 `failed`，无需独立 `/status` 子路径。

**Response 200：**

```json
{
  "data": {
    "id": "uuid",
    "knowledge_base_id": "uuid",
    "filename": "report.pdf",
    "file_type": "pdf",
    "file_size": 204800,
    "status": "processing",
    "version": 1,
    "current_task_type": "embed",
    "chunk_count": 0,
    "error_code": null,
    "error_message": null,
    "retry_count": 0,
    "delete_cycle": 0,
    "processing_started_at": "2025-01-01T00:00:02Z",
    "processing_finished_at": null,
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:00:10Z",
    "allowed_actions": ["delete"]
  }
}
```

**错误码：**

- `404 NOT_FOUND`

---

##### `GET /knowledge-bases/{kb_id}/documents/{doc_id}/tasks` — 获取文档处理任务

> 用于排障或高级状态展示。普通前端轮询优先使用文档详情接口；需要展示具体卡在 parse / chunk / embed 哪一步时再调用此接口。

**Response 200：**

```json
{
  "data": {
    "items": [
      {
        "id": "uuid",
        "document_id": "uuid",
        "document_version": 1,
        "task_type": "embed",
        "status": "running",
        "retry_count": 0,
        "max_retries": 3,
        "total_items": 43,
        "processed_items": 20,
        "queued_at": "2025-01-01T00:00:02Z",
        "started_at": "2025-01-01T00:00:10Z",
        "finished_at": null,
        "error_message": null,
        "created_at": "2025-01-01T00:00:02Z",
        "updated_at": "2025-01-01T00:00:20Z",
        "attempts": []
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20
  }
}
```

**错误码：**

- `404 NOT_FOUND`

---

##### `DELETE /knowledge-bases/{kb_id}/documents/{doc_id}` — 删除文档

> DELETE 使用所属用户范围的独立变更查询。首次调用将资料更新为 `deleting`、取消未开始任务，并为本次 `delete_cycle` 创建专用 `delete_cleanup` 后返回成功；
> 从提交起资料不再出现在列表、详情和检索中。若有运行 attempt，则保留处理 lease 并以其
> `expires_at` 为冻结的等待上限，资料进入 deleting 后心跳不得续租；所有持久化写入通过
> `attempt_id` fencing 在同一事务校验 attempt/task
> 仍运行且资料未删除。worker 卡死时，孤儿扫描器超时后取消 attempt/task、释放名额并激活清理。
> `delete_cleanup` 清理原文件、草稿、chunks 和向量后保留最小 `deleted` 墓碑；历史引用只返回快照。若其重试耗尽，
> 资料以 `failed` / `current_task_type=delete_cleanup` / `20015` 返回最小“删除未完成”墓碑，`allowed_actions` 仅为
> `retry_delete`；从该失败状态再次调用本 DELETE 创建新清理任务，不得重置旧任务或 attempt。若资料已为
> `deleting`，重复调用幂等成功且不递增 `delete_cycle`、不创建任务；资料已为 `deleted` 时返回 404。

**删除未完成资料示例（Response 200，仅资料所属用户可见）：**

```json
{
  "data": {
    "id": "uuid",
    "knowledge_base_id": "uuid",
    "filename": "report.pdf",
    "file_type": "pdf",
    "file_size": 204800,
    "status": "failed",
    "version": 1,
    "current_task_type": "delete_cleanup",
    "retry_count": 3,
    "delete_cycle": 2,
    "chunk_count": 0,
    "error_code": 20015,
    "error_message": "资料删除未完成，请重试删除",
    "processing_started_at": null,
    "processing_finished_at": null,
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:03:00Z",
    "allowed_actions": ["retry_delete"]
  }
}
```

**Response 200：** 返回统一成功信封，`data` 为 `null`

**错误码：**

- `404 NOT_FOUND`

---

#### 模块四：对话

##### `GET /conversations` — 获取对话列表

**Query Params：**

|参数|类型|说明|
|---|---|---|
|page|int|页码，默认 1|
|page_size|int|每页数量，默认 20|

**Response 200：**

```json
{
  "data": {
    "items": [
      {
        "id": "uuid",
        "title": "关于 Q3 报告的问题",
        "knowledge_base_id": "uuid",
        "last_message_at": "2025-01-02T10:00:00Z",
        "created_at": "2025-01-01T00:00:00Z"
      }
    ],
    "total": 8,
    "page": 1,
    "page_size": 20
  }
}
```

---

##### `POST /conversations` — 创建对话

**Request Body：**

```json
{
  "knowledge_base_id": "uuid",
  "title": "关于 Q3 报告的问题"
}
```

> `knowledge_base_id` 必填，且必须属于当前认证用户；MVP 不支持未绑定知识库的纯对话模式。`title` 可为空，后端在首条消息后自动生成。

**Response 201：**

```json
{
  "data": {
    "id": "uuid",
    "title": null,
    "knowledge_base_id": "uuid",
    "last_message_at": null,
    "created_at": "2025-01-01T00:00:00Z"
  }
}
```

---

##### `GET /conversations/{conv_id}` — 获取对话详情

**Response 200：** 返回单个对话对象

**错误码：**

- `404 NOT_FOUND`

---

##### `PATCH /conversations/{conv_id}` — 更新对话（如重命名标题）

**Request Body：**

```json
{
  "title": "新标题"
}
```

**Response 200：** 返回更新后的对话对象

---

##### `DELETE /conversations/{conv_id}` — 删除对话

**Response 200：** 返回统一成功信封，`data` 为 `null`

---

##### `GET /conversations/{conv_id}/messages` — 获取历史消息

**Query Params：**

|参数|类型|说明|
|---|---|---|
|before|string|游标（message_id），返回此条消息之前的记录|
|limit|int|返回条数，默认 50，最大 100|

**Response 200：**

```json
{
  "data": {
    "items": [
      {
        "id": "uuid",
        "role": "user",
        "status": "completed",
        "content": "Q3 的销售额是多少？",
        "rewritten_query": null,
        "finish_reason": null,
        "created_at": "2025-01-01T00:00:00Z"
      },
      {
        "id": "uuid",
        "role": "assistant",
        "status": "completed",
        "content": "根据报告，Q3 销售额为 1200 万元……",
        "rewritten_query": null,
        "finish_reason": "stop",
        "created_at": "2025-01-01T00:01:00Z"
      }
    ],
    "has_more": false,
    "next_before": null
  }
}
```

> citations 不在列表中内嵌，由前端在用户点击"查看来源"时按需拉取（见下方接口），避免响应体过重。

---

##### `POST /conversations/{conv_id}/messages` — 发送消息（SSE 流式）

> 核心接口，触发完整 RAG 链路。响应为 **SSE 流**（Server-Sent Events）。

**Request Body：**

```json
{
  "content": "Q3 的销售额是多少？"
}
```

**Response 200：** `Content-Type: text/event-stream`

```
event: message_start
data: {"code":0,"data":{"message_id":"uuid"},"msg":"","trace_id":"uuid"}

event: retrieval_done
data: {"code":0,"data":{"citations":[{"rank":1,"score":0.92,"chunk_id":"uuid","document_id":"uuid","document_version":1,"filename":"report.pdf","file_type":"pdf","page":3,"section":null,"content":"Q3 销售总额达 1200 万元……","source_type":"live"}]},"msg":"","trace_id":"uuid"}

event: delta
data: {"code":0,"data":{"text":"根据"},"msg":"","trace_id":"uuid"}

event: delta
data: {"code":0,"data":{"text":"报告，"},"msg":"","trace_id":"uuid"}

event: delta
data: {"code":0,"data":{"text":"Q3 销售额为 1200 万元……"},"msg":"","trace_id":"uuid"}

event: message_end
data: {"code":0,"data":{"message_id":"uuid","finish_reason":"stop"},"msg":"","trace_id":"uuid"}
```

**SSE 事件约定：**

|事件|触发时机|data|
|---|---|---|
|`message_start`|assistant 消息创建成功|统一信封，`data.message_id`|
|`retrieval_done`|检索、融合、重排完成|统一信封，`data.citations` 预览列表|
|`delta`|模型生成增量文本|统一信封，`data.text`|
|`message_end`|生成正常结束|统一信封，`data.message_id`、`data.finish_reason`|
|`error`|生成前或生成中失败|错误信封，`code`、`msg`、`trace_id`|

客户端连接断开时，服务端应取消后续 LLM 生成和未完成的流式写入；若 assistant message 已创建但未完整生成，必须持久化为 `status = cancelled`、`finish_reason = cancelled`。供应商、模型或服务错误在重试耗尽后必须发送 `error` 事件，并持久化为 `status = failed`、`finish_reason = error`；正常完成或可信无证据答复为 `completed/stop`。API 进程异常退出时，由既有维护扫描器在 `MESSAGE_STREAMING_STALE_SECONDS`（默认 360 秒）后将仍为 `streaming` 的记录条件更新为 `failed/error`。任何分支都不得删除已创建消息或留下 `streaming` 状态。

#### 请求限流的来源 IP

认证限流默认使用 TCP 直连对端 IP，并忽略客户端提供的全部转发头。只有直连对端属于
`RATE_LIMIT_TRUSTED_PROXY_CIDRS` 显式配置的代理网段时，才解析 `X-Forwarded-For`：把直连对端
附在链尾，从右向左跳过可信代理，选择首个非可信地址。配置默认值为空，任一地址格式非法或链中不存在非可信地址时
回退直连对端 IP；不信任 `X-Real-IP`，日志、Redis 和指标不得保存完整转发链。

#### 镜像发布与回滚

GitHub Actions 使用 `GITHUB_TOKEN` 的 `packages: write` 权限发布
`ghcr.io/${GITHUB_REPOSITORY}-backend` 与 `ghcr.io/${GITHUB_REPOSITORY}-frontend`，其中仓库名在
workflow 中统一转为小写。受保护分支使用不可变 `sha-${GITHUB_SHA}` 标签，正式 Git tag 可追加
语义版本标签；Compose 通过 `BACKEND_IMAGE`、`FRONTEND_IMAGE` 接收完整引用，不使用 `latest`。
回滚时把两个变量同时切换到上一已验证 SHA 标签，再执行 pull、up 和健康检查。镜像回滚不自动
降级数据库；破坏性迁移发布前必须人工备份，失败时停止发布并按备份恢复。数据库迁移只能由
部署流程中的单次串行 one-off 后端容器执行 `alembic upgrade head`，API/worker 启动不得自动迁移；
迁移成功后才切换运行容器，迁移失败保持旧容器运行。

**RAG 链路（后端执行顺序）：**

如果知识库内至少存在 1 篇 `completed` 文档，则允许对话；仍在 `pending` / `queued` / `processing` 的新文档暂不参与检索。如果 completed 文档数为 0，返回 `409 KNOWLEDGE_BASE_NOT_READY`。

1. 查询改写（默认结合近 3 轮历史消息）
2. 向量召回（pgvector cosine，Top-K=10）
3. 关键词召回（默认 pg_trgm，Top-K=10；可选中文 FTS）
4. RRF 融合排序
5. 重排序（Reranker）
6. 上下文打包（去重 + token 截断，MVP 默认最多 3000 tokens）
7. 流式生成回答

**向量召回 SQL 约束：**

```sql
SELECT
  c.id,
  c.document_id,
  c.knowledge_base_id,
  c.user_id,
  c.content,
  c.metadata,
  c.document_version,
  1 - (c.embedding <=> $3::vector) AS score
FROM chunks c
JOIN documents d ON d.id = c.document_id
WHERE c.user_id = $1
  AND c.knowledge_base_id = $2
  AND c.document_version = d.version
  AND d.status = 'completed'
ORDER BY c.embedding <=> $3::vector
LIMIT 10;
```

**关键词召回 SQL 约束（MVP 默认 pg_trgm）：**

```sql
SELECT
  c.id,
  c.document_id,
  c.knowledge_base_id,
  c.user_id,
  c.content,
  c.metadata,
  c.document_version,
  similarity(c.content, $3) AS score
FROM chunks c
JOIN documents d ON d.id = c.document_id
WHERE c.user_id = $1
  AND c.knowledge_base_id = $2
  AND c.document_version = d.version
  AND d.status = 'completed'
  AND c.content % $3
ORDER BY score DESC
LIMIT 10;
```

**中文 FTS 召回 SQL 约束（可选增强）：**

```sql
SELECT
  c.id,
  c.document_id,
  c.knowledge_base_id,
  c.user_id,
  c.content,
  c.metadata,
  c.document_version,
  ts_rank_cd(c.content_ts, plainto_tsquery('jieba', $3)) AS score
FROM chunks c
JOIN documents d ON d.id = c.document_id
WHERE c.user_id = $1
  AND c.knowledge_base_id = $2
  AND c.document_version = d.version
  AND d.status = 'completed'
  AND c.content_ts @@ plainto_tsquery('jieba', $3)
ORDER BY score DESC
LIMIT 10;
```

`$1` 必须来自当前认证用户，`$2` 必须来自当前会话的 `knowledge_base_id`，`$3` 是查询向量或查询文本。RRF 融合和 rerank 前需要再次断言候选 chunk 都属于同一 `user_id`、`knowledge_base_id` 和当前 `document_version`。

**错误码：**

- `404 NOT_FOUND` — 对话不存在
- `409 KNOWLEDGE_BASE_NOT_READY` — 当前会话绑定的知识库中没有任何 `completed` 文档

---

##### `GET /conversations/{conv_id}/messages/{msg_id}/citations` — 获取消息引用来源

> 前端在用户点击"查看来源"时按需调用，不在消息列表中预加载。
>
> 查询时必须先校验 `messages -> conversations.user_id = current_user_id`，再按 `message_citations.chunk_id` / `document_id` 校验引用来源仍属于当前用户和当前知识库。即使引用表已有 `message_id`，也不能跳过会话归属校验。
>
> 引用展示优先读取活表 `chunks` / `documents` 的当前可见信息；如果 `chunk_id` 或 `document_id` 因文档删除、cleanup 或重处理后被置为 `NULL`，或源文档已不可访问，则从 `message_citations.chunk_snapshot` 返回历史快照。这样历史回答仍能展示当时引用的文件名、页码、章节和内容预览。

**Response 200：**

```json
{
  "data": {
    "items": [
      {
        "rank": 1,
        "score": 0.9234567891234,
        "chunk_id": "uuid",
        "document_id": "uuid",
        "document_version": 1,
        "filename": "report.pdf",
        "file_type": "pdf",
        "page": 3,
        "section": "第二章 引言",
        "content": "……Q3 销售总额达 1200 万元，同比增长 15%……",
        "source_type": "live"
      }
    ],
    "page": 1,
    "page_size": 20,
    "total": 1
  }
}
```

**错误码：**

- `404 NOT_FOUND`

---

### HTTP 状态码使用规范

|场景|状态码|
|---|---|
|创建资源成功|`201 Created`|
|异步任务已接受（文档上传）|`202 Accepted`|
|查询 / 更新成功|`200 OK`|
|删除成功|`200 OK`，返回统一成功信封且 `data = null`|
|参数校验失败|`400 Bad Request`|
|未携带 Token / Token 无效|`401 Unauthorized`|
|按 ID 访问他人知识库|`404 Not Found`（`20002`；不得全局探测归属）|
|按 ID 访问他人资料、任务、对话、消息或引用|`404 Not Found`（`20007`；不得全局探测归属）|
|资源不存在|`404 Not Found`|
|资源冲突（邮箱重复等）|`409 Conflict`|
|服务端内部错误|`500 Internal Server Error`|

---

### 错误码总表

|Code|HTTP Status|说明|
|---|---|---|
|`INVALID_REQUEST`|400|请求参数校验失败|
|`UNSUPPORTED_FILE_TYPE`（`20009`）|400|仅支持 PDF、DOCX、MD 和 TXT 文件|
|`DOCUMENT_PARSE_FAILED`（`20001`）|详情 HTTP 200|资料解析失败，请删除后重新上传|
|`EMPTY_DOCUMENT`（`20010`）|详情 HTTP 200|资料内容为空，请删除后重新上传；不得进入分块或嵌入|
|`DOCUMENT_STORAGE_FAILED`（`20011`）|详情 HTTP 200|文件保存失败，请删除后重新上传|
|`DOCUMENT_EMBEDDING_FAILED`（`20012`）|详情 HTTP 200|资料向量化失败，请删除后重新上传|
|`DOCUMENT_FINALIZE_FAILED`（`20013`）|详情 HTTP 200|资料处理结果不一致，请删除后重新上传|
|`DOCUMENT_RETRY_EXHAUSTED`（`20014`）|详情 HTTP 200|资料处理失败，请删除后重新上传|
|`DELETE_CLEANUP_FAILED`（`20015`）|详情 HTTP 200|资料或知识库删除未完成，请重试删除|
|`FILE_TOO_LARGE`|400|文件超过大小限制（50MB）|
|`TOO_MANY_FILES`|400|单次上传超过 20 个文件；整批无副作用|
|`INVALID_CREDENTIALS`|401|邮箱或密码错误|
|`UNAUTHORIZED`|401|Token 无效或已过期|
|`INVALID_REFRESH_TOKEN`（`10006`）|401|refresh_token 无效、过期、已撤销或发生重放|
|`FORBIDDEN`|403|资源已在当前授权边界内，但当前主体仍无该操作权限；不得用于区分跨租户资源是否存在|
|`NOT_FOUND`|404|资源不存在|
|`EMAIL_ALREADY_EXISTS`|409|邮箱已注册|
|`KNOWLEDGE_BASE_NOT_READY`|409|当前知识库没有任何 `completed` 文档|
|`INTERNAL_ERROR`|500|服务端内部错误|
