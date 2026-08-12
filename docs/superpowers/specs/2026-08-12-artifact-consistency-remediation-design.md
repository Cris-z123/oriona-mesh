# OrionaMesh 高严重度规划产物一致性修复设计

## 目标

关闭 I-001、I-002、C-001、C-002、C-003、C-004，使 `docs/OrionaMesh.md`、逻辑数据模型、
OpenAPI、Research、Quickstart 与 Tasks 对 MVP 的技术决策、数据生命周期和客户端契约给出唯一答案。

## I-001：设计决策统一

以已认可的 Spec 与 Plan 为最终决策更新 `docs/OrionaMesh.md`：

- 后端结构化日志统一使用 structlog，不再列出 Loguru。
- Access Token 默认 2 小时，响应 `expires_in = 7200`；Refresh Token 7 天。
- SSE 中断后保留已创建的 assistant 消息并收敛为 `cancelled`，不再保留“删除或取消二选一”。
- pg_trgm 是 MVP 必需的关键词召回与部署就绪检查项，不再列入 Phase 2 才实现的能力。

## I-002：租户字段与知识库删除生命周期

- `conversations.knowledge_base_id` 必须为 `NOT NULL`，外键使用 `ON DELETE CASCADE`。
- 删除知识库时级联删除绑定对话与消息，不产生知识库为空的纯聊天或只读归档状态。
- `messages` 增加防御性 `user_id`，并增加 `(user_id, conversation_id, created_at)` 查询索引。
- `message_citations` 增加防御性 `user_id` 与 `knowledge_base_id`，所有引用查询先按当前用户和
  会话授权；资料删除时仍可通过 citation 快照核验，知识库/对话删除时 citation 随消息级联删除。
- `data-model.md` 保持逻辑模型定位，并明确上述外键、非空与删除关系必须由 ORM/Alembic 实现。

## C-001：分页契约

- 知识库、资料、资料任务、对话和引用列表使用页码分页：`page` 默认 1，`page_size` 默认 20、
  最大 100，响应包含 `items/page/page_size/total`。
- 资料列表额外支持可选 `status` 过滤。
- 消息历史使用游标分页：`before` 为可选 message UUID，`limit` 默认 50、最大 100；响应包含
  `items/has_more/next_before`，按创建时间倒序取数后按展示顺序返回。
- OpenAPI 为每个列表操作显式声明查询参数，不再让消息复用通用 `MessagePage`。

## C-002：资料与任务响应 DTO

`Document` 使用与逻辑模型一致的字段：

- `id`、`knowledge_base_id`、`filename`、`file_type`、`file_size`
- `status`、`version`、`current_task_type`、`retry_count`、`chunk_count`
- `error_message`、`processing_started_at`、`processing_finished_at`
- `created_at`、`updated_at`、`allowed_actions`

`allowed_actions` 为枚举数组；失败资料在 MVP 中只允许 `delete`，不得出现 reprocess/replace。

`DocumentTask` 补齐：`document_version`、`task_type`、`status`、`retry_count`、`max_retries`、
`total_items`、`processed_items`、`error_message`、`queued_at`、`started_at`、`finished_at`、
`created_at`、`updated_at` 和结构化 `attempts`。字段名统一使用数据模型术语，不再暴露
`current_stage/failure_reason/document_version` 作为 Document 的同义字段。

## C-003：认证和上传错误契约

- `10004/401 INVALID_CREDENTIALS` 只用于登录邮箱或密码错误。
- 新增 `10006/401 INVALID_REFRESH_TOKEN`，固定提示“登录状态已失效，请重新登录”，供刷新令牌
  无效、过期、撤销或重放场景使用。
- 新增 `20009/400 UNSUPPORTED_FILE_TYPE`，固定提示“仅支持 PDF、DOCX、MD 和 TXT 文件”。
- 上传校验响应允许 `20003`、`20004`、`20009`；实现任务和契约测试必须逐一断言。
- 更新统一 ErrorCode 枚举、Research 错误码表、Quickstart 和前端提示任务。

## C-004：SSE 类型契约

OpenAPI 定义以下事件对象：

- `MessageStartEvent`：`event = message_start`，信封 data 包含 `message_id`。
- `RetrievalDoneEvent`：`event = retrieval_done`，信封 data 包含引用预览数组。
- `DeltaEvent`：`event = delta`，信封 data 包含增量 `text`。
- `MessageEndEvent`：`event = message_end`，信封 data 包含 `message_id` 与 `finish_reason`。
- `StreamErrorEvent`：`event = error`，错误信封包含稳定业务码、消息与 `trace_id`。

`text/event-stream` 响应使用 `oneOf` 和 `event` discriminator 表达事件集合，并提供合法 SSE 文本
示例。契约测试逐事件验证信封、必填字段、终态和禁止正文泄露。

## 同步与验证

同步修改：

- `docs/OrionaMesh.md`
- `specs/001-orionamesh-rag-mvp/data-model.md`
- `specs/001-orionamesh-rag-mvp/research.md`
- `specs/001-orionamesh-rag-mvp/contracts/openapi.yaml`
- `specs/001-orionamesh-rag-mvp/quickstart.md`
- `specs/001-orionamesh-rag-mvp/tasks.md`

验证包括：OpenAPI 组件引用完整、所有列表参数显式存在、消息游标响应不再引用 `MessagePage`、
Document/Task 必需字段和枚举完整、错误码唯一、五类 SSE schema 可引用、任务编号连续且所有
任务引用有效，以及设计文档不再出现已废弃决策。

