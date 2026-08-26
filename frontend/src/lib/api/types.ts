/**
 * API 客户端类型（T108）：镜像 `specs/001-orionamesh-rag-mvp/contracts/openapi.yaml`
 * 的统一信封与 DTO。前端只消费冻结契约；状态、错误码与 allowed_actions 均由服务端返回，
 * 客户端不得复制或推导后端规则。
 */

/** 统一响应信封：code=0 为成功；非 0 为业务错误码。 */
export interface ApiEnvelope<T> {
  code: number;
  data: T;
  msg: string;
  trace_id: string;
}

/** 页码分页结果（openapi Pagination）。 */
export interface Page<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}

export interface User {
  id: string;
  email: string;
  display_name: string | null;
}

/** 登录/刷新返回的会话令牌；refresh_token 明文仅返回一次。 */
export interface SessionTokens {
  access_token: string;
  refresh_token: string;
  token_type: "Bearer";
  expires_in: number;
}

export type KnowledgeBaseStatus = "active" | "delete_failed";
export type ResourceAction = "delete" | "retry_delete";

/**
 * 知识库：active 返回完整对象；delete_failed 为最小“删除未完成”墓碑
 * （name/description 为 null，allowed_actions 仅为 retry_delete）。
 */
export interface KnowledgeBase {
  id: string;
  name: string | null;
  description: string | null;
  status: KnowledgeBaseStatus;
  delete_error_code: number | null;
  allowed_actions: ResourceAction[];
  created_at: string;
  updated_at: string;
}

export type DocumentStatus = "pending" | "queued" | "processing" | "completed" | "failed";
export type DocumentTaskType =
  "parse" | "chunk" | "embed" | "finalize" | "cleanup" | "delete_cleanup";
export type DocumentFileType = "pdf" | "docx" | "md" | "txt";
export type DocumentTaskStatus =
  "pending" | "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type DocumentTaskAttemptStatus = "running" | "succeeded" | "failed" | "cancelled";

/** 资料列表的非敏感视图偏好：状态过滤（"all" 表示不过滤）。 */
export type DocumentStatusFilter = DocumentStatus | "all";

/**
 * 资料详情：异步失败以 HTTP 200 + error_code/error_message 表达；
 * failed/delete_cleanup/20015 为最小“删除未完成”墓碑（allowed_actions 仅为 retry_delete）。
 */
export interface Document {
  id: string;
  knowledge_base_id: string;
  filename: string;
  file_type: DocumentFileType;
  file_size: number;
  status: DocumentStatus;
  version: number;
  current_task_type: DocumentTaskType | null;
  retry_count: number;
  delete_cycle: number;
  chunk_count: number;
  error_code: number | null;
  error_message: string | null;
  processing_started_at: string | null;
  processing_finished_at: string | null;
  created_at: string;
  updated_at: string;
  allowed_actions: ResourceAction[];
}

/** 资料处理尝试：字段与 DocumentTaskAttempt 契约一一对应。 */
export interface DocumentTaskAttempt {
  id: string;
  task_id: string;
  attempt_no: number;
  worker_name: string | null;
  status: DocumentTaskAttemptStatus;
  started_at: string;
  finished_at: string | null;
  error_message: string | null;
  duration_ms: number | null;
  created_at: string;
}

/** 资料处理任务：服务端状态、进度、失败原因和尝试记录均为唯一真相。 */
export interface DocumentTask {
  id: string;
  document_id: string;
  document_version: number;
  task_type: DocumentTaskType;
  delete_cycle: number;
  status: DocumentTaskStatus;
  retry_count: number;
  max_retries: number;
  total_items: number | null;
  processed_items: number;
  error_code: number | null;
  error_message: string | null;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
  attempts: DocumentTaskAttempt[];
}

/** 批量上传 202 结果；每项只能是 queued 或 failed/20011。 */
export interface DocumentUploadResult {
  documents: Document[];
}

/** 绑定知识库的连续问答容器；标题为空时由界面显示“未命名对话”。 */
export interface Conversation {
  id: string;
  knowledge_base_id: string;
  /** 当前用户授权关联投影的所属知识库名称（T172，非持久化字段）。 */
  knowledge_base_name: string;
  title: string | null;
  last_message_at: string | null;
  created_at: string;
  updated_at: string;
}

export type AssistantMessageStatus = "streaming" | "completed" | "failed" | "cancelled";
export type AssistantFinishReason = "stop" | "length" | "error" | "cancelled" | null;

export interface UserMessage {
  id: string;
  conversation_id: string;
  role: "user";
  content: string;
  status: "completed";
  rewritten_query: string | null;
  finish_reason: null;
  created_at: string;
}

export interface AssistantMessage {
  id: string;
  conversation_id: string;
  role: "assistant";
  content: string;
  status: AssistantMessageStatus;
  rewritten_query: null;
  finish_reason: AssistantFinishReason;
  created_at: string;
}

export type Message = UserMessage | AssistantMessage;

/** 游标分页消息历史；服务端顺序和 next_before 为唯一可信依据。 */
export interface MessageCursorPage {
  items: Message[];
  has_more: boolean;
  next_before: string | null;
}

/** 引用 DTO 不包含持久化 ID；UI 仅以消息内的 rank 作为短生命周期抽屉选择器。 */
export interface Citation {
  rank: number;
  score: number;
  chunk_id: string | null;
  document_id: string | null;
  document_version: number;
  filename: string;
  file_type: DocumentFileType;
  page: number | null;
  section: string | null;
  content: string;
  source_type: "live" | "snapshot";
}

/** 上传约束（FR-024/FR-025，与 openapi 描述一致；服务端仍为最终执行者）。 */
export const UPLOAD_LIMITS = {
  maxFileBytes: 50 * 1024 * 1024,
  maxFiles: 20,
} as const;

/** SSE 判别事件名（openapi SseEvent）。 */
export type SseEventName = "message_start" | "retrieval_done" | "delta" | "message_end" | "error";

/** 固定业务错误码（openapi ErrorCode 子集；仅用于展示分支，不推导规则）。 */
export const ERROR_CODES = {
  TOKEN_EXPIRED: 10001,
  FORBIDDEN: 10002,
  INVALID_REQUEST: 10003,
  INVALID_CREDENTIALS: 10004,
  RATE_LIMIT_EXCEEDED: 10005,
  INVALID_REFRESH_TOKEN: 10006,
  DOCUMENT_PARSE_FAILED: 20001,
  KNOWLEDGE_BASE_NOT_FOUND: 20002,
  FILE_TOO_LARGE: 20003,
  TOO_MANY_FILES: 20004,
  KNOWLEDGE_BASE_NOT_READY: 20005,
  EMAIL_ALREADY_EXISTS: 20006,
  RESOURCE_NOT_FOUND: 20007,
  RESOURCE_CONFLICT: 20008,
  UNSUPPORTED_FILE_TYPE: 20009,
  EMPTY_DOCUMENT: 20010,
  DOCUMENT_STORAGE_FAILED: 20011,
  DOCUMENT_EMBEDDING_FAILED: 20012,
  DOCUMENT_FINALIZE_FAILED: 20013,
  DOCUMENT_RETRY_EXHAUSTED: 20014,
  DELETE_CLEANUP_FAILED: 20015,
  KNOWLEDGE_BASE_NAME_ALREADY_EXISTS: 20016,
  INTERNAL_ERROR: 50000,
} as const;
