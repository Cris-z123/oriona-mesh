/**
 * API 客户端（T108）：只消费 `specs/001-orionamesh-rag-mvp/contracts/openapi.yaml` 冻结契约。
 *
 * - 统一信封：成功 code=0 返回 data；同步业务错误按 code 抛 ApiError（含 msg/trace_id）；
 * - HTTP 200 详情内的异步 error_code/error_message 作为 DTO 字段原样透传；
 * - Bearer 会话；10001/401 自动用 refresh token 轮换一次并重放（并发去重）；
 * - 登出携带 Bearer + refresh token 请求体；
 * - 上传：请求级 Idempotency-Key（显式或自动生成）、xhr.upload 进度回调、20008/409 冲突；
 * - 分页 page/page_size；限流 10005/429 携带 Retry-After；
 * - SSE：解析 `event:`/`data:` 文本帧并按判别事件分发。
 */
import { clearSession, getSession, setSession } from "@/lib/api/session";
import type {
  ApiEnvelope,
  Citation,
  Conversation,
  Document,
  DocumentStatus,
  DocumentTask,
  DocumentUploadResult,
  KnowledgeBase,
  MessageCursorPage,
  Page,
  SessionTokens,
  User,
} from "@/lib/api/types";

const DEFAULT_BASE_URL = "/v1";
const ERROR_CODE_SESSION_EXPIRED = 10001;
const ERROR_CODE_INTERNAL = 50000;
const INTERNAL_ERROR_MSG = "系统繁忙，请稍后再试";

function baseUrl(): string {
  return process.env.NEXT_PUBLIC_API_BASE_URL ?? DEFAULT_BASE_URL;
}

function qs(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.append(key, String(value));
  }
  return search.toString();
}

export class ApiError extends Error {
  readonly code: number;
  readonly status: number;
  readonly msg: string;
  readonly traceId: string | null;
  readonly retryAfter: number | null;

  constructor(params: {
    code: number;
    msg: string;
    status: number;
    traceId: string | null;
    retryAfter?: number | null;
  }) {
    super(params.msg);
    this.name = "ApiError";
    this.code = params.code;
    this.status = params.status;
    this.msg = params.msg;
    this.traceId = params.traceId;
    this.retryAfter = params.retryAfter ?? null;
  }
}

function isEnvelope(raw: unknown): raw is ApiEnvelope<unknown> {
  if (typeof raw !== "object" || raw === null) return false;
  const e = raw as Record<string, unknown>;
  return (
    typeof e.code === "number" &&
    "data" in e &&
    typeof e.msg === "string" &&
    typeof e.trace_id === "string"
  );
}

async function parseEnvelope<T>(response: Response): Promise<ApiEnvelope<T> | null> {
  try {
    const raw: unknown = await response.json();
    return isEnvelope(raw) ? (raw as ApiEnvelope<T>) : null;
  } catch {
    return null;
  }
}

function parseEnvelopeText<T>(text: string): ApiEnvelope<T> | null {
  try {
    const raw: unknown = JSON.parse(text);
    return isEnvelope(raw) ? (raw as ApiEnvelope<T>) : null;
  } catch {
    return null;
  }
}

function parseRetryAfter(raw: string | null): number | null {
  if (!raw) return null;
  const seconds = Number.parseInt(raw, 10);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

function toApiError(
  status: number,
  envelope: ApiEnvelope<unknown> | null,
  retryAfterRaw: string | null = null
): ApiError {
  if (envelope) {
    return new ApiError({
      code: envelope.code,
      msg: envelope.msg,
      status,
      traceId: envelope.trace_id,
      retryAfter: parseRetryAfter(retryAfterRaw),
    });
  }
  return new ApiError({
    code: ERROR_CODE_INTERNAL,
    msg: INTERNAL_ERROR_MSG,
    status,
    traceId: null,
  });
}

/** 把任意异常归一为 ApiError（未分类异常按 50000 处理，组件可直接渲染）。 */
export function asApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err;
  return new ApiError({
    code: ERROR_CODE_INTERNAL,
    msg: INTERNAL_ERROR_MSG,
    status: 0,
    traceId: null,
  });
}

function applySession(tokens: SessionTokens): void {
  setSession({
    accessToken: tokens.access_token,
    refreshToken: tokens.refresh_token,
    expiresAt: Date.now() + tokens.expires_in * 1000,
  });
}

/** 轮换刷新：并发请求共享同一进行中的刷新，失败不清除其他路径的会话状态。 */
let refreshInFlight: Promise<boolean> | null = null;

async function refreshOnce(): Promise<boolean> {
  const session = getSession();
  if (!session?.refreshToken) return false;
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    try {
      const res = await fetch(`${baseUrl()}/auth/sessions`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: session.refreshToken }),
      });
      const envelope = await parseEnvelope<SessionTokens>(res);
      if (res.status !== 200 || !envelope || envelope.code !== 0) return false;
      applySession(envelope.data);
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** 认证端点（注册/登录/刷新）不携带 Bearer 且不触发自动刷新。 */
  noAuth?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<ApiEnvelope<T>> {
  const { method = "GET", body, noAuth = false } = options;
  const session = noAuth ? null : getSession();
  const headers: Record<string, string> = {};
  if (!noAuth && session) headers.Authorization = `Bearer ${session.accessToken}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const res = await fetch(`${baseUrl()}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const envelope = await parseEnvelope<T>(res);
    if (res.ok && envelope && envelope.code === 0) return envelope;

    const canRefresh =
      attempt === 0 &&
      !noAuth &&
      res.status === 401 &&
      envelope?.code === ERROR_CODE_SESSION_EXPIRED;
    if (canRefresh) {
      if (await refreshOnce()) {
        headers.Authorization = `Bearer ${getSession()?.accessToken ?? ""}`;
        continue;
      }
      clearSession();
    }
    throw toApiError(res.status, envelope, res.headers.get("Retry-After"));
  }
  throw new ApiError({
    code: ERROR_CODE_INTERNAL,
    msg: INTERNAL_ERROR_MSG,
    status: 0,
    traceId: null,
  });
}

// ---------------------------------------------------------------------------
// 认证与会话（FR-001）
// ---------------------------------------------------------------------------

export async function register(input: {
  email: string;
  password: string;
  display_name?: string;
}): Promise<User> {
  const envelope = await request<User>("/users", { method: "POST", body: input, noAuth: true });
  return envelope.data;
}

export async function login(input: { email: string; password: string }): Promise<SessionTokens> {
  const envelope = await request<SessionTokens>("/auth/sessions", {
    method: "POST",
    body: input,
    noAuth: true,
  });
  applySession(envelope.data);
  return envelope.data;
}

/** 登出：Bearer 定位用户，请求体携带 refresh token；成功后清除本地会话。 */
export async function logout(): Promise<void> {
  try {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const session = getSession();
      if (!session) return;

      const res = await fetch(`${baseUrl()}/auth/sessions`, {
        method: "DELETE",
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          "Content-Type": "application/json",
        },
        // 每次重试都读取当前会话，确保刷新轮换后撤销的是新建的后继会话。
        body: JSON.stringify({ refresh_token: session.refreshToken }),
      });
      const envelope = await parseEnvelope<null>(res);
      if (res.ok && envelope?.code === 0) return;

      const canRefresh =
        attempt === 0 && res.status === 401 && envelope?.code === ERROR_CODE_SESSION_EXPIRED;
      if (canRefresh && (await refreshOnce())) continue;

      throw toApiError(res.status, envelope, res.headers.get("Retry-After"));
    }
  } finally {
    clearSession();
  }
}

// ---------------------------------------------------------------------------
// 当前用户（FR-002）
// ---------------------------------------------------------------------------

export async function getMe(): Promise<User> {
  const envelope = await request<User>("/users/me");
  return envelope.data;
}

export async function updateMe(input: { display_name: string }): Promise<User> {
  const envelope = await request<User>("/users/me", { method: "PATCH", body: input });
  return envelope.data;
}

// ---------------------------------------------------------------------------
// 知识库（FR-003）
// ---------------------------------------------------------------------------

export async function listKnowledgeBases(page = 1, pageSize = 20): Promise<Page<KnowledgeBase>> {
  const envelope = await request<Page<KnowledgeBase>>(
    `/knowledge-bases?${qs({ page, page_size: pageSize })}`
  );
  return envelope.data;
}

export async function createKnowledgeBase(input: {
  name: string;
  description?: string;
}): Promise<KnowledgeBase> {
  const envelope = await request<KnowledgeBase>("/knowledge-bases", {
    method: "POST",
    body: input,
  });
  return envelope.data;
}

export async function updateKnowledgeBase(
  id: string,
  input: { name?: string; description?: string }
): Promise<KnowledgeBase> {
  const envelope = await request<KnowledgeBase>(`/knowledge-bases/${id}`, {
    method: "PATCH",
    body: input,
  });
  return envelope.data;
}

export async function deleteKnowledgeBase(id: string): Promise<void> {
  await request<null>(`/knowledge-bases/${id}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// 会话、消息与引用（FR-013～FR-019；阶段 9）
// ---------------------------------------------------------------------------

export async function listConversations(
  knowledgeBaseId: string,
  page = 1,
  pageSize = 20
): Promise<Page<Conversation>> {
  const envelope = await request<Page<Conversation>>(
    `/conversations?${qs({ knowledge_base_id: knowledgeBaseId, page, page_size: pageSize })}`
  );
  return envelope.data;
}

export async function createConversation(input: {
  knowledge_base_id: string;
  title?: string;
}): Promise<Conversation> {
  const envelope = await request<Conversation>("/conversations", { method: "POST", body: input });
  return envelope.data;
}

export async function getConversation(id: string): Promise<Conversation> {
  const envelope = await request<Conversation>(`/conversations/${id}`);
  return envelope.data;
}

export async function renameConversation(
  id: string,
  input: { title: string }
): Promise<Conversation> {
  const envelope = await request<Conversation>(`/conversations/${id}`, {
    method: "PATCH",
    body: input,
  });
  return envelope.data;
}

export async function deleteConversation(id: string): Promise<void> {
  await request<null>(`/conversations/${id}`, { method: "DELETE" });
}

export async function listMessages(
  conversationId: string,
  before?: string,
  limit = 50
): Promise<MessageCursorPage> {
  const envelope = await request<MessageCursorPage>(
    `/conversations/${conversationId}/messages?${qs({ before, limit })}`
  );
  return envelope.data;
}

export async function listCitations(
  conversationId: string,
  messageId: string,
  page = 1,
  pageSize = 20
): Promise<Page<Citation>> {
  const envelope = await request<Page<Citation>>(
    `/conversations/${conversationId}/messages/${messageId}/citations?${qs({ page, page_size: pageSize })}`
  );
  return envelope.data;
}

// ---------------------------------------------------------------------------
// 资料（FR-004/005/010/011/024/025/031）
// ---------------------------------------------------------------------------

export async function listDocuments(
  knowledgeBaseId: string,
  page = 1,
  pageSize = 20,
  status?: DocumentStatus
): Promise<Page<Document>> {
  const envelope = await request<Page<Document>>(
    `/knowledge-bases/${knowledgeBaseId}/documents?${qs({ page, page_size: pageSize, status })}`
  );
  return envelope.data;
}

export async function getDocument(knowledgeBaseId: string, documentId: string): Promise<Document> {
  const envelope = await request<Document>(
    `/knowledge-bases/${knowledgeBaseId}/documents/${documentId}`
  );
  return envelope.data;
}

export async function deleteDocument(knowledgeBaseId: string, documentId: string): Promise<void> {
  await request<null>(`/knowledge-bases/${knowledgeBaseId}/documents/${documentId}`, {
    method: "DELETE",
  });
}

/** 获取资料处理任务及其尝试记录；DTO 内的状态与错误码直接供界面呈现。 */
export async function listDocumentTasks(
  knowledgeBaseId: string,
  documentId: string,
  page = 1,
  pageSize = 20
): Promise<Page<DocumentTask>> {
  const envelope = await request<Page<DocumentTask>>(
    `/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/tasks?${qs({ page, page_size: pageSize })}`
  );
  return envelope.data;
}

/** 契约格式：^[A-Za-z0-9._:-]{8,128}$（openapi Idempotency-Key）。 */
export function generateIdempotencyKey(): string {
  const bytes = new Uint8Array(16);
  if (
    typeof globalThis.crypto !== "undefined" &&
    typeof globalThis.crypto.getRandomValues === "function"
  ) {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

export interface UploadOptions {
  /** 显式请求级幂等键；缺省自动生成。 */
  idempotencyKey?: string;
  /** xhr.upload 进度回调；lengthComputable 时按字节比例渲染。 */
  onProgress?: (loaded: number, total: number) => void;
}

export function uploadDocuments(
  knowledgeBaseId: string,
  files: File[],
  options: UploadOptions = {}
): Promise<DocumentUploadResult> {
  const idempotencyKey = options.idempotencyKey ?? generateIdempotencyKey();
  return new Promise<DocumentUploadResult>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${baseUrl()}/knowledge-bases/${knowledgeBaseId}/documents`);
    const session = getSession();
    if (session) xhr.setRequestHeader("Authorization", `Bearer ${session.accessToken}`);
    xhr.setRequestHeader("Idempotency-Key", idempotencyKey);
    if (options.onProgress) {
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) options.onProgress?.(event.loaded, event.total);
      };
    }
    xhr.onload = () => {
      const envelope = parseEnvelopeText<DocumentUploadResult>(xhr.responseText);
      if (xhr.status >= 200 && xhr.status < 300 && envelope && envelope.code === 0) {
        resolve(envelope.data);
      } else {
        reject(toApiError(xhr.status, envelope, xhr.getResponseHeader("Retry-After")));
      }
    };
    xhr.onerror = () => {
      reject(
        new ApiError({
          code: ERROR_CODE_INTERNAL,
          msg: INTERNAL_ERROR_MSG,
          status: 0,
          traceId: null,
        })
      );
    };
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    xhr.send(form);
  });
}

// ---------------------------------------------------------------------------
// SSE（T108 客户端封装；会话/消息 UI 于阶段 9 消费）
// ---------------------------------------------------------------------------

export interface SseOptions {
  body?: unknown;
  signal?: AbortSignal;
  onEvent: (event: string, data: ApiEnvelope<Record<string, unknown>>) => void;
}

/** 解析 SSE `event:`/`data:` 文本帧；data 为统一信封 JSON。 */
export async function streamEvents(path: string, options: SseOptions): Promise<void> {
  const session = getSession();
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (session) headers.Authorization = `Bearer ${session.accessToken}`;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(`${baseUrl()}${path}`, {
    method: "POST",
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  });
  if (!res.ok || !res.body) {
    const envelope = await parseEnvelope(res);
    throw toApiError(res.status, envelope, res.headers.get("Retry-After"));
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const frameSeparator = /\r?\n\r?\n/;
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let match = frameSeparator.exec(buffer);
    while (match) {
      dispatchFrame(buffer.slice(0, match.index), options.onEvent);
      buffer = buffer.slice(match.index + match[0].length);
      match = frameSeparator.exec(buffer);
    }
  }
  if (buffer.trim() !== "") dispatchFrame(buffer, options.onEvent);
}

function dispatchFrame(
  frame: string,
  onEvent: (event: string, data: ApiEnvelope<Record<string, unknown>>) => void
): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const rawLine of frame.split("\n")) {
    const line = rawLine.trimEnd();
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }
  if (dataLines.length === 0) return;
  const raw: unknown = JSON.parse(dataLines.join("\n"));
  if (!isEnvelope(raw)) return;
  onEvent(event, raw as ApiEnvelope<Record<string, unknown>>);
}
