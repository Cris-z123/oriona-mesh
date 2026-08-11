# OrionaMesh

一个工程级的 C 端 RAG 开源应用。

OrionaMesh 面向个人和轻量团队场景，核心目标是把本地或私有资料构建成可检索、可追溯、可持续更新的个人知识库，并在对话中提供带引用来源的回答。

## 产品设计

### 产品定位

- **项目形态**：开源应用，不是纯基础设施库。
- **核心场景**：用户上传 PDF、Word、Markdown、Text 等文本资料，构建个人知识库，并基于知识库连续问答。
- **核心原则**：用户级数据隔离、异步文档处理、检索可追溯、历史对话可复查。

### 产品路径

1. 用户上传`pdf`、`word`、`markdown`、`text`等文本类资料，构建知识库，用户级别隔离知识库
2. 参考知识库的情况下，用户可以连续对话

### 产品功能

- 支持用户批量上传资料，构建个人知识库，可以增删改查源文件
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

MVP 阶段先使用固定策略，后续再抽成可配置项。

|环节|MVP 默认策略|说明|
|---|---|---|
|Query Rewrite|结合最近 3 轮历史消息，用低成本 LLM 改写|只处理省略指代、上下文补全，不主动扩展问题范围|
|向量召回|Top-K=10|基于 `pgvector` cosine distance|
|关键词召回|Top-K=10|MVP 默认使用 `pg_trgm`；`pg_jieba` / `zhparser` 作为可选增强|
|融合排序|RRF|合并双路召回结果，按 `chunk_id` 去重|
|Reranker|MVP 可先用 API 或本地 bge-reranker-base|若暂未接入 reranker，则直接使用 RRF 结果|
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
- 召回结果为空时，不应直接生成普通回答；应返回“知识库中未找到相关内容”，并允许用户换问法或检查文档处理状态。

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
- Loguru

#### Deploy

- Docker
- Github

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
              └── chunks (1:N，完成态分块，含 embedding 向量)

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
    last_login_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

|字段|类型|说明|
|---|---|---|
|id|UUID|主键|
|email|VARCHAR(255)|唯一，登录凭证|
|password_hash|VARCHAR(255)|bcrypt 哈希|
|display_name|VARCHAR(100)|展示名称，可为空|
|created_at / updated_at|TIMESTAMPTZ|创建/更新时间|
|last_login_at|TIMESTAMPTZ|最近一次登录成功时间，登录接口成功后更新|

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

- access token 使用短有效期 JWT，MVP 默认 1 小时。
- refresh token 使用随机高熵字符串，只在 `auth_sessions.refresh_token_hash` 中保存哈希。
- `PUT /auth/sessions` 刷新时必须轮换 refresh token：旧 session 标记 `revoked_at`，创建新 session，并通过 `rotated_from_session_id` 关联。
- `DELETE /auth/sessions` 登出时按当前 refresh token 找到 session 并置 `revoked_at`。
- 如果已撤销 session 的 refresh token 再次被使用，视为疑似重放，可撤销该用户当前所有 active sessions。

---

##### 2. knowledge_bases — 知识库表

```sql
CREATE TABLE knowledge_bases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        VARCHAR(200) NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_kb_user_id ON knowledge_bases(user_id);
```

|字段|类型|说明|
|---|---|---|
|id|UUID|主键|
|user_id|UUID|外键，用户隔离核心字段|
|name|VARCHAR(200)|知识库名称|
|description|TEXT|描述，可为空|

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
    status            VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- pending | queued | processing | completed | failed | deleting | deleted
    error_message     TEXT,
    chunk_count       INTEGER DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 1,
    current_task_type VARCHAR(32),
    retry_count       INTEGER NOT NULL DEFAULT 0,
    processing_started_at  TIMESTAMPTZ,
    processing_finished_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_doc_kb_id   ON documents(knowledge_base_id);
CREATE INDEX idx_doc_user_id ON documents(user_id);
CREATE INDEX idx_doc_status  ON documents(status);
```

|字段|类型|说明|
|---|---|---|
|status|VARCHAR|`pending` → `processing` → `completed` / `failed`|
|storage_path|TEXT|原始文件存储路径，MVP 为本地磁盘相对路径，例如 `uploads/{user_id}/{document_id}/source.pdf`|
|chunk_count|INTEGER|入库成功后回写，方便展示|
|version|INTEGER|文档内容版本，重新上传、编辑或重建分块时递增|
|current_task_type|VARCHAR|当前处理阶段：`parse` / `chunk` / `embed` / `finalize`|
|retry_count|INTEGER|文档级重试次数汇总，方便前端和排障展示|
|processing_started_at / processing_finished_at|TIMESTAMPTZ|处理开始/结束时间，用于耗时统计|

> `documents.status` 是用户视角状态，适合前端列表和详情轮询；任务内部阶段以 `document_tasks.status` 为准。`current_task_type` 表示当前或即将执行的流水线阶段，因此文档刚创建且状态仍为 `pending` / `queued` 时可以显示为 `parse`；`completed` 后置为 `null`；`failed` 时保留失败阶段，方便用户知道卡在哪一步。

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
- 正式 `chunks` 只保存可检索的完成态数据，草稿分块放在 `document_chunk_drafts`。

---

##### 5. conversations — 对话表

```sql
CREATE TABLE conversations (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    knowledge_base_id UUID REFERENCES knowledge_bases(id) ON DELETE SET NULL,
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
|knowledge_base_id|UUID|可为空，表示不绑定知识库（纯对话模式）|
|title|VARCHAR|首轮消息自动截取或 AI 生成|
|last_message_at|TIMESTAMPTZ|与 updated_at 分离，专用于"最近对话"排序|

---

##### 6. messages — 消息表

```sql
CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
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
```

|字段|类型|说明|
|---|---|---|
|status|VARCHAR|消息状态。user 消息通常直接为 `completed`；assistant 流式生成时先为 `streaming`|
|finish_reason|VARCHAR|assistant 结束原因：`stop` / `length` / `error` / `cancelled`|

---

##### 7. message_citations — 引用溯源表

```sql
CREATE TABLE message_citations (
    id          UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id  UUID             NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chunk_id    UUID             REFERENCES chunks(id) ON DELETE SET NULL,
    document_id UUID             REFERENCES documents(id) ON DELETE SET NULL,
    -- ↑ 冗余存储，避免前端展示引用时的多层 JOIN（chunks → documents）；源数据删除后保留快照
    score       DOUBLE PRECISION,  -- 召回融合分数（0~1），FLOAT 精度不足
    rank        INTEGER,            -- 最终展示顺序（1 = 最相关）
    document_version INTEGER NOT NULL,
    chunk_snapshot JSONB
);

CREATE INDEX idx_cite_msg_id ON message_citations(message_id);
```

> 此表支持"查看引用来源"功能，方便用户核查 AI 回答出处。`document_id` 冗余存储是有意为之，前端展示"来源：report.pdf 第3页"时无需多层 JOIN。`chunk_snapshot` 用于保存引用时刻的少量快照，避免文档后续重建或删除后历史回答失去可读来源。

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
    -- parse | chunk | embed | finalize | cleanup

    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- pending | queued | running | succeeded | failed | cancelled

    idempotency_key VARCHAR(128) NOT NULL,
    task_payload JSONB NOT NULL DEFAULT '{}',
    task_result JSONB,
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
|task_type|VARCHAR|处理阶段：`parse` / `chunk` / `embed` / `finalize` / `cleanup`|
|status|VARCHAR|任务状态：`pending` / `queued` / `running` / `succeeded` / `failed` / `cancelled`|
|idempotency_key|VARCHAR|幂等键，建议格式：`{task_type}:{document_id}:v{document_version}`|
|total_items / processed_items|INTEGER|任务处理总量和已完成数量，例如 embed 已写入 chunk 数|
|checkpoint|JSONB|任务恢复点，例如当前批次、最后处理的 `chunk_index`、外部任务 ID|
|depends_on_task_id|UUID|上游任务依赖，用于表达 parse → chunk → embed → finalize|

> `documents.status` 用于前端展示，`document_tasks.status` 用于系统内部任务编排。Celery 重投递时必须先按 `idempotency_key` 查重。

---

##### 9. document_task_attempts — 文档任务尝试记录表

```sql
CREATE TABLE document_task_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES document_tasks(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    worker_name VARCHAR(128),
    status VARCHAR(20) NOT NULL,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_task_attempts_task_id
ON document_task_attempts(task_id);

CREATE UNIQUE INDEX uniq_task_attempt_no
ON document_task_attempts(task_id, attempt_no);
```

> 此表是排障视角状态：记录每次任务尝试、失败原因、worker 信息和耗时。MVP 可以先只写失败记录，但表结构建议保留。

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

> `document_chunk_drafts` 保存 chunk 阶段的中间结果。只有 embed 成功后的完成态分块才写入 `chunks`，这样 retrieval 不需要处理 `embedding IS NULL` 或半成品数据。

---

### 关键实现约束

#### 文档处理状态机

```text
pending
-> queued
-> processing
-> completed
-> failed
-> deleting
-> deleted
```

- 上传成功后先创建 `documents`，再创建 `parse` 任务。
- 进入任务队列时更新 `documents.status = queued`。
- 文档创建或排队时可预填 `current_task_type = parse`；任一任务运行时更新 `documents.status = processing` 和对应的 `current_task_type`。
- `finalize` 成功后统一回写 `completed`、`chunk_count`、`processing_finished_at`。
- `cleanup` 在 `finalize` 之后异步触发，用于清理旧版本派生数据；检索正确性不依赖 cleanup。
- 删除文档时进入 `deleting`，取消未完成任务并清理草稿，完成后进入 `deleted` 或直接物理删除。

#### 事务边界

- `parse`：提交解析结果，不写正式 `chunks`。
- `chunk`：写 `document_chunk_drafts`，不写 embedding。
- `embed`：按批次写正式 `chunks`，例如每 50 / 100 个 chunk 一个事务。
- `finalize`：单独事务回写 `documents`。
- `cleanup`：删除或归档旧版本 `chunks`、`document_chunk_drafts`、`document_parse_results`。

Embedding 调用不应包在长事务里，避免外部模型延迟导致数据库连接被长时间占用。

#### 上传与任务创建一致性

对象存储和数据库不能放在同一个强事务里，MVP 使用补偿式一致性：

1. 上传文件先写入临时对象路径，例如 `tmp/{user_id}/{uuid}`。
2. 数据库事务内创建 `documents` 和第一条 `parse` 类型 `document_tasks`。
3. 事务提交成功后，将临时对象移动或标记为正式 `storage_path`。
4. 如果数据库事务失败，删除临时对象。
5. 如果对象转正失败，将 `documents.status` 更新为 `failed` 并写入 `error_message`。

任务调度以数据库为真相源：Celery 只负责执行任务，不能作为唯一状态来源。worker 必须能定期扫描 `document_tasks.status IN ('pending', 'queued')` 的任务并执行，避免 Celery 投递失败导致文档永久停留在 `pending`。

#### embedding 幂等入库

`embed` 任务必须按 `document_id + document_version + chunk_strategy_version + embedding_model + chunk_index` 幂等写入正式 `chunks`。

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

#### 并发与限流

- 单用户同时处于 `processing` 的文档默认最多 3 个，超过后任务保持 `queued`。
- 单文档同一 `document_version` 同一 `task_type` 只能存在一个未终态任务，由 `idempotency_key` 保证。
- embedding 阶段按批次处理，单批建议 50 / 100 个 chunk，失败只重试当前任务，不回滚已成功的其他文档。
- 用户删除文档时，后续未开始任务标记为 `cancelled`；运行中任务在阶段边界检查到 `documents.status = deleting` 后停止继续写入。

#### 降级策略

- Reranker 不可用：跳过 rerank，直接使用 RRF 融合结果进入 Context Pack。
- 中文 FTS 扩展不可用：MVP 必须退化为 `pg_trgm` 关键词召回；如果 `pg_trgm` 也不可用，则部署检查失败，不进入可用状态。
- Query Rewrite 失败：回退到用户原始问题继续检索。
- SSE 生成中断：已保存的 user message 保留；assistant message 若未完成，标记 `finish_reason = cancelled` 或不落库，二者在实现时择一并保持一致。

#### MVP 实现阶段

1. **Phase 1：文档入库闭环**
   - 用户注册 / 登录
   - 知识库 CRUD
   - 文档上传、对象存储、异步任务表
   - parse / chunk / embed / finalize / cleanup
   - 基础向量召回

2. **Phase 2：检索质量增强**
   - 文档 reprocess API
   - 文档源文件替换 API
   - pg_trgm 关键词召回
   - 可选中文 FTS 召回
   - RRF 融合
   - Reranker
   - Context Pack
   - 引用来源落库

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
{ "data": { ... } }
```

```json
{ "error": { "code": "NOT_FOUND", "message": "知识库不存在" } }
```

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
    "refresh_token": "eyJ...",
    "expires_in": 3600,
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
> 刷新成功时必须轮换 refresh token：旧 session 写入 `revoked_at`，新 session 通过 `rotated_from_session_id` 指向旧 session。

**Request Body：**

```json
{
  "refresh_token": "eyJ..."
}
```

**Response 200：**

```json
{
  "data": {
    "access_token": "eyJ...",
    "refresh_token": "eyJ...",
    "expires_in": 3600
  }
}
```

**错误码：**

- `401 INVALID_TOKEN` — refresh_token 无效或已过期

---

##### `DELETE /auth/sessions` — 登出

> 使当前 refresh_token 失效（Redis 黑名单或直接删除）。
>
> MVP 以 `auth_sessions.revoked_at` 为准；Redis 只作为可选加速层，不作为唯一失效依据。

**Response 204：** No Content

---

##### 忘记密码 / 密码重置

MVP 不支持忘记密码、邮件重置或验证码重置流程。用户遗失密码时，由管理员手动重置 `users.password_hash`，或删除账号后由用户重新注册。

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
        "document_count": 12,
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
    "document_count": 0,
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

> 级联删除该知识库下所有文档和 chunks。

**Response 204：** No Content

**错误码：**

- `404 NOT_FOUND`

---

#### 模块三：文档

##### `POST /knowledge-bases/{kb_id}/documents` — 上传文档

**Content-Type：** `multipart/form-data`

|字段|类型|是否必须|说明|
|---|---|---|---|
|file|File|是|支持 PDF / DOCX / MD / TXT|
|filename|string|否|覆盖原始文件名|

**Response 202：**（异步处理，立即返回，状态为 `pending` 或 `queued`）

```json
{
  "data": {
    "id": "uuid",
    "knowledge_base_id": "uuid",
    "filename": "report.pdf",
    "file_type": "pdf",
    "file_size": 204800,
    "status": "pending",
    "version": 1,
    "current_task_type": "parse",
    "chunk_count": 0,
    "processing_started_at": null,
    "processing_finished_at": null,
    "created_at": "2025-01-01T00:00:00Z"
  }
}
```

**错误码：**

- `400 UNSUPPORTED_FILE_TYPE` — 不支持的文件类型
- `400 FILE_TOO_LARGE` — 文件超过大小限制（MVP 默认 50MB）
- `404 NOT_FOUND` — 知识库不存在

---

##### `GET /knowledge-bases/{kb_id}/documents` — 获取文档列表

**Query Params：**

|参数|类型|说明|
|---|---|---|
|page|int|页码，默认 1|
|page_size|int|每页数量，默认 20，最大 100|
|status|string|可选，按状态过滤|

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
        "chunk_count": 43,
        "error_message": null,
        "processing_started_at": "2025-01-01T00:00:02Z",
        "processing_finished_at": "2025-01-01T00:01:00Z",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:01:00Z"
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
    "error_message": null,
    "retry_count": 0,
    "processing_started_at": "2025-01-01T00:00:02Z",
    "processing_finished_at": null,
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:00:10Z"
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
        "queued_at": "2025-01-01T00:00:02Z",
        "started_at": "2025-01-01T00:00:10Z",
        "finished_at": null,
        "error_message": null
      }
    ]
  }
}
```

**错误码：**

- `404 NOT_FOUND`

---

##### `DELETE /knowledge-bases/{kb_id}/documents/{doc_id}` — 删除文档

> 删除时先取消未完成任务并清理 `document_chunk_drafts`，再级联删除该文档所有 chunks 及向量数据。若文档正在处理，服务端可先将 `documents.status` 更新为 `deleting`。

**Response 204：** No Content

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

> `knowledge_base_id` 可为 null（纯对话模式）；`title` 可为空，后端在首条消息后自动生成。

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

**Response 204：** No Content

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
        "content": "Q3 的销售额是多少？",
        "rewritten_query": null,
        "created_at": "2025-01-01T00:00:00Z"
      },
      {
        "id": "uuid",
        "role": "assistant",
        "content": "根据报告，Q3 销售额为 1200 万元……",
        "rewritten_query": null,
        "created_at": "2025-01-01T00:01:00Z"
      }
    ],
    "has_more": false
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
data: {"message_id": "uuid"}

event: retrieval_done
data: {"citations": [{"rank": 1, "score": 0.92, "chunk_id": "uuid", "document_id": "uuid", "filename": "report.pdf", "page": 3}]}

event: delta
data: {"text": "根据"}

event: delta
data: {"text": "报告，"}

event: delta
data: {"text": "Q3 销售额为 1200 万元……"}

event: message_end
data: {"message_id": "uuid", "finish_reason": "stop"}
```

**SSE 事件约定：**

|事件|触发时机|data|
|---|---|---|
|`message_start`|assistant 消息创建成功|`message_id`|
|`retrieval_done`|检索、融合、重排完成|`citations` 预览列表|
|`delta`|模型生成增量文本|`text`|
|`message_end`|生成正常结束|`message_id`、`finish_reason`|
|`error`|生成前或生成中失败|`code`、`message`|

客户端断开连接时，服务端应取消后续 LLM 生成和未完成的流式写入。若 assistant message 已创建但未完整生成，必须使用统一策略处理：要么保存为 cancelled 状态，要么删除未完成 assistant message，不能留下无结束状态的半截消息。

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
        "content": "……Q3 销售总额达 1200 万元，同比增长 15%……"
      }
    ]
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
|删除成功|`204 No Content`|
|参数校验失败|`400 Bad Request`|
|未携带 Token / Token 无效|`401 Unauthorized`|
|无权访问他人资源|`403 Forbidden`|
|资源不存在|`404 Not Found`|
|资源冲突（邮箱重复等）|`409 Conflict`|
|服务端内部错误|`500 Internal Server Error`|

---

### 错误码总表

|Code|HTTP Status|说明|
|---|---|---|
|`INVALID_REQUEST`|400|请求参数校验失败|
|`UNSUPPORTED_FILE_TYPE`|400|不支持的文件格式|
|`FILE_TOO_LARGE`|400|文件超过大小限制（50MB）|
|`INVALID_CREDENTIALS`|401|邮箱或密码错误|
|`UNAUTHORIZED`|401|Token 无效或已过期|
|`INVALID_TOKEN`|401|refresh_token 无效|
|`FORBIDDEN`|403|无权访问该资源|
|`NOT_FOUND`|404|资源不存在|
|`EMAIL_ALREADY_EXISTS`|409|邮箱已注册|
|`KNOWLEDGE_BASE_NOT_READY`|409|当前知识库没有任何 `completed` 文档|
|`INTERNAL_ERROR`|500|服务端内部错误|
