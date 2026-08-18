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

/** 批量上传 202 结果；每项只能是 queued 或 failed/20011。 */
export interface DocumentUploadResult {
  documents: Document[];
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
  INTERNAL_ERROR: 50000,
} as const;
