import type { Page, Route } from "@playwright/test";

import type {
  ApiEnvelope,
  Citation,
  Conversation,
  Document,
  DocumentTask,
  DocumentTaskAttempt,
  KnowledgeBase,
  SessionTokens,
  User,
} from "@/lib/api/types";

const TRACE_ID = "00000000-0000-4000-8000-000000000001";
const NOW = "2026-08-19T00:00:00Z";

export interface ApiMockResponse {
  status?: number;
  data?: unknown;
  code?: number;
  msg?: string;
  headers?: Record<string, string>;
}

export type ApiMockHandler = (route: Route) => ApiMockResponse | Promise<ApiMockResponse>;

/** 以方法和路径为键，让每个端到端场景显式声明自己的接口响应。 */
export type ApiMockHandlers = Record<string, ApiMockHandler>;

export function apiEnvelope<T>(data: T, traceId = TRACE_ID): ApiEnvelope<T> {
  return { code: 0, data, msg: "ok", trace_id: traceId };
}

export function apiError(
  status: number,
  code: number,
  msg: string,
  headers: Record<string, string> = {}
): ApiMockResponse {
  return { status, code, msg, data: null, headers };
}

/** 符合契约的 SSE 事件帧；每个事件载荷都保持统一 API 信封。 */
export function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(apiEnvelope(data))}\n\n`;
}

/** 终态错误帧使用非零业务码，与冻结 SSE 错误信封一致。 */
export function sseErrorFrame(
  event: string,
  code: number,
  msg: string,
  data: unknown = {}
): string {
  return `event: ${event}\ndata: ${JSON.stringify({ code, data, msg, trace_id: TRACE_ID })}\n\n`;
}

export function sseResponse(events: string[]): ApiMockResponse {
  return {
    data: events.join(""),
    headers: { "content-type": "text/event-stream; charset=utf-8" },
  };
}

function documentTaskAttemptFixture(
  overrides: Partial<DocumentTaskAttempt> = {}
): DocumentTaskAttempt {
  return {
    id: "00000000-0000-4000-8000-000000000041",
    task_id: "00000000-0000-4000-8000-000000000040",
    attempt_no: 1,
    worker_name: "e2e-worker",
    status: "succeeded",
    started_at: NOW,
    finished_at: NOW,
    error_message: null,
    duration_ms: 1,
    created_at: NOW,
    ...overrides,
  };
}

/**
 * Install a route for exactly the configured versioned API origin. Unhandled
 * requests get the contract's resource-not-found response, never a real API.
 */
export async function installApiMock(page: Page, handlers: ApiMockHandlers = {}): Promise<void> {
  const fulfill = async (route: Route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") {
      await route.fulfill({
        status: 204,
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-headers": "Authorization, Content-Type, Idempotency-Key",
          "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
        },
      });
      return;
    }
    const url = new URL(request.url());
    const key = `${request.method()} ${url.pathname}`;
    const handler = handlers[key] ?? handlers[`${request.method()} *`];
    const response = handler ? await handler(route) : apiError(404, 20007, "请求的资源不存在");
    const contentType = response.headers?.["content-type"] ?? "application/json";
    const body = contentType.startsWith("text/event-stream")
      ? String(response.data ?? "")
      : JSON.stringify({
          code: response.code ?? 0,
          data: response.data ?? null,
          msg: response.msg ?? "ok",
          trace_id: TRACE_ID,
        });

    await route.fulfill({
      status: response.status ?? 200,
      contentType,
      headers: {
        "access-control-allow-origin": "*",
        "access-control-expose-headers": "Retry-After",
        ...response.headers,
      },
      body,
    });
  };
  // 公开环境变量由 Next 在构建时注入，开发期可能是 localhost 或 127.0.0.1。
  // 所有版本化 API Origin 都被拦截，确保 E2E 不会命中任何真实后端。
  await page.route("**/v1/**", fulfill);
}

export const apiFixtures = {
  user(overrides: Partial<User> = {}): User {
    return {
      id: "00000000-0000-4000-8000-000000000010",
      email: "reader@example.com",
      display_name: "Reader",
      ...overrides,
    };
  },
  session(overrides: Partial<SessionTokens> = {}): SessionTokens {
    return {
      access_token: "test-access-token",
      refresh_token: "rt_0123456789abcdefghijklmnopqrstuvwxABCDEFGHI",
      token_type: "Bearer",
      expires_in: 7200,
      ...overrides,
    };
  },
  knowledgeBase(overrides: Partial<KnowledgeBase> = {}): KnowledgeBase {
    return {
      id: "00000000-0000-4000-8000-000000000020",
      name: "测试知识库",
      description: "契约替身",
      status: "active",
      delete_error_code: null,
      allowed_actions: ["delete"],
      created_at: NOW,
      updated_at: NOW,
      ...overrides,
    };
  },
  document(overrides: Partial<Document> = {}): Document {
    return {
      id: "00000000-0000-4000-8000-000000000030",
      knowledge_base_id: "00000000-0000-4000-8000-000000000020",
      filename: "notes.md",
      file_type: "md",
      file_size: 42,
      status: "completed",
      version: 1,
      current_task_type: null,
      retry_count: 0,
      delete_cycle: 0,
      chunk_count: 1,
      error_code: null,
      error_message: null,
      processing_started_at: NOW,
      processing_finished_at: NOW,
      created_at: NOW,
      updated_at: NOW,
      allowed_actions: ["delete"],
      ...overrides,
    };
  },
  documentTaskAttempt(overrides: Partial<DocumentTaskAttempt> = {}): DocumentTaskAttempt {
    return documentTaskAttemptFixture(overrides);
  },
  documentTask(overrides: Partial<DocumentTask> = {}): DocumentTask {
    const id = overrides.id ?? "00000000-0000-4000-8000-000000000040";
    return {
      id,
      document_id: "00000000-0000-4000-8000-000000000030",
      document_version: 1,
      task_type: "parse",
      delete_cycle: 0,
      status: "succeeded",
      retry_count: 0,
      max_retries: 3,
      total_items: 1,
      processed_items: 1,
      error_code: null,
      error_message: null,
      queued_at: NOW,
      started_at: NOW,
      finished_at: NOW,
      created_at: NOW,
      updated_at: NOW,
      attempts: [documentTaskAttemptFixture({ task_id: id })],
      ...overrides,
    };
  },
  conversation(overrides: Partial<Conversation> = {}): Conversation {
    return {
      id: "00000000-0000-4000-8000-000000000050",
      knowledge_base_id: "00000000-0000-4000-8000-000000000020",
      title: "测试对话",
      last_message_at: NOW,
      created_at: NOW,
      updated_at: NOW,
      ...overrides,
    };
  },
  citation(overrides: Partial<Citation> = {}): Citation {
    return {
      rank: 1,
      score: 0.9,
      chunk_id: "00000000-0000-4000-8000-000000000061",
      document_id: "00000000-0000-4000-8000-000000000030",
      document_version: 1,
      filename: "notes.md",
      file_type: "md",
      page: null,
      section: "摘要",
      content: "资料片段预览。",
      source_type: "live",
      ...overrides,
    };
  },
};
