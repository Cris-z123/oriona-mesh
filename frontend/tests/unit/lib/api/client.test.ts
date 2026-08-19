import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  createKnowledgeBase,
  deleteDocument,
  deleteKnowledgeBase,
  getDocument,
  getMe,
  listKnowledgeBases,
  listDocuments,
  login,
  logout,
  register,
  streamEvents,
  updateMe,
  uploadDocuments,
} from "@/lib/api/client";
import { clearSession, getSession, setSession } from "@/lib/api/session";

/**
 * T106 [P] [US1] API 客户端失败测试（先写后验）。
 *
 * 覆盖冻结契约（contracts/openapi.yaml）的客户端封装：
 * - 统一信封：code=0 返回 data；非 0 按 code 抛同步 ApiError（含 msg/trace_id）；
 * - HTTP 200 详情内异步 error_code/error_message 原样透传（如 Document.status=failed）；
 * - 上传 Idempotency-Key（显式键 + 自动生成键格式）+ 协调中 20008/409；
 * - page/page_size 分页参数与分页结果形状；
 * - 限流 10005/429 与 Retry-After；
 * - 10001 自动刷新一次并重放、刷新失败清除会话；
 * - 登出携带 Bearer + refresh token 请求体并清除本地会话；
 * - SSE 文本帧解析（T108 客户端封装）。
 */
const TEST_TRACE_ID = "7eb23f43-e1f4-4a67-a64d-1a481b36030f";

function envelope(code: number, data: unknown, msg = "", traceId = TEST_TRACE_ID) {
  return { code, data, msg, trace_id: traceId };
}

function fetchResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    headers: new Headers(headers),
  } as Response;
}

const fetchMock = vi.fn<typeof fetch>();

/** 假 XHR：记录请求、触发 upload 进度并投递队列中的响应（支持顺序多个响应）。 */
class FakeXHR {
  static queue: { status: number; body: unknown; headers?: Record<string, string> }[] = [];
  static instances: FakeXHR[] = [];
  static enqueue(status: number, body: unknown, headers?: Record<string, string>) {
    FakeXHR.queue.push({ status, body, headers });
  }
  static last(): FakeXHR {
    return FakeXHR.instances[FakeXHR.instances.length - 1]!;
  }
  method = "";
  url = "";
  status = 0;
  responseText = "";
  requestHeaders: Record<string, string> = {};
  upload: {
    onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null;
  } = {
    onprogress: null,
  };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.requestHeaders[name] = value;
  }
  send() {
    FakeXHR.instances.push(this);
    const r = FakeXHR.queue.shift() ?? {
      status: 500,
      body: envelope(50000, null, "系统繁忙，请稍后再试"),
    };
    this.status = r.status;
    this.responseText = JSON.stringify(r.body);
    this.upload.onprogress?.({ lengthComputable: true, loaded: 40, total: 100 });
    this.onload?.();
  }
  getResponseHeader(): string | null {
    return null;
  }
}

beforeEach(() => {
  clearSession();
  FakeXHR.queue = [];
  FakeXHR.instances = [];
  vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://api.test/v1");
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("XMLHttpRequest", FakeXHR);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const USER = { id: "u1", email: "a@example.com", display_name: "Alice" };
const KB = {
  id: "kb-1",
  name: "笔记",
  description: null,
  status: "active",
  delete_error_code: null,
  allowed_actions: ["delete"],
  created_at: "2026-08-18T00:00:00Z",
  updated_at: "2026-08-18T00:00:00Z",
};
const DOC = {
  id: "d1",
  knowledge_base_id: "kb-1",
  filename: "a.pdf",
  file_type: "pdf",
  file_size: 100,
  status: "queued",
  version: 1,
  current_task_type: "parse",
  retry_count: 0,
  delete_cycle: 0,
  chunk_count: 0,
  error_code: null,
  error_message: null,
  processing_started_at: null,
  processing_finished_at: null,
  created_at: "2026-08-18T00:00:00Z",
  updated_at: "2026-08-18T00:00:00Z",
  allowed_actions: ["delete"],
};

describe("API 客户端：统一信封", () => {
  it("成功响应返回 data（code=0 不抛错）", async () => {
    fetchMock.mockResolvedValue(fetchResponse(200, envelope(0, USER)));
    await expect(getMe()).resolves.toEqual(USER);
  });

  it("同步业务错误按 code 抛 ApiError，携带 msg 与 trace_id", async () => {
    fetchMock.mockResolvedValue(fetchResponse(401, envelope(10004, null, "邮箱或密码错误")));
    const err = await login({ email: "a@example.com", password: "wrong" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe(10004);
    expect((err as ApiError).msg).toBe("邮箱或密码错误");
    expect((err as ApiError).traceId).toBe(TEST_TRACE_ID);
    expect((err as ApiError).status).toBe(401);
  });

  it("登录成功持久化会话（access/refresh token）", async () => {
    fetchMock.mockResolvedValue(
      fetchResponse(
        201,
        envelope(0, {
          access_token: "at-1",
          refresh_token: "rt_abc",
          token_type: "Bearer",
          expires_in: 7200,
        })
      )
    );
    await login({ email: "a@example.com", password: "pw-123456" });
    const s = getSession();
    expect(s?.accessToken).toBe("at-1");
    expect(s?.refreshToken).toBe("rt_abc");
    expect(s?.expiresAt).toBeGreaterThan(Date.now());
  });
});

describe("API 客户端：异步 error_code 与 HTTP 状态解耦", () => {
  it("详情 HTTP 200 内异步 error_code/error_message 原样透传", async () => {
    const failed = {
      ...DOC,
      status: "failed",
      error_code: 20001,
      error_message: "资料解析失败，请删除后重新上传",
    };
    fetchMock.mockResolvedValue(fetchResponse(200, envelope(0, failed)));
    const doc = await getDocument("kb-1", "d1");
    expect(doc.status).toBe("failed");
    expect(doc.error_code).toBe(20001);
    expect(doc.error_message).toBe("资料解析失败，请删除后重新上传");
    expect(doc.allowed_actions).toEqual(["delete"]);
  });

  it("列表 HTTP 200 内 delete_failed/20015 墓碑保持最小形状", async () => {
    const tombstone = {
      ...KB,
      name: null,
      description: null,
      status: "delete_failed",
      delete_error_code: 20015,
      allowed_actions: ["retry_delete"],
    };
    fetchMock.mockResolvedValue(
      fetchResponse(200, envelope(0, { items: [tombstone], page: 1, page_size: 20, total: 1 }))
    );
    const page = await listKnowledgeBases();
    expect(page.total).toBe(1);
    expect(page.items[0]?.name).toBeNull();
    expect(page.items[0]?.status).toBe("delete_failed");
    expect(page.items[0]?.allowed_actions).toEqual(["retry_delete"]);
  });
});

describe("API 客户端：上传幂等与协调 409", () => {
  const file = new File(["hello"], "a.txt", { type: "text/plain" });

  it("显式 Idempotency-Key 原样发送，202 返回已排队资料", async () => {
    FakeXHR.enqueue(202, envelope(0, { documents: [DOC] }));
    const result = await uploadDocuments("kb-1", [file], { idempotencyKey: "key-abc-123" });
    expect(result.documents[0]?.status).toBe("queued");
    const xhr = FakeXHR.last();
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("http://api.test/v1/knowledge-bases/kb-1/documents");
    expect(xhr.requestHeaders["Idempotency-Key"]).toBe("key-abc-123");
  });

  it("未提供幂等键时自动生成并满足契约格式", async () => {
    FakeXHR.enqueue(202, envelope(0, { documents: [DOC] }));
    await uploadDocuments("kb-1", [file]);
    const key = FakeXHR.last().requestHeaders["Idempotency-Key"];
    expect(key).toMatch(/^[A-Za-z0-9._:-]{8,128}$/);
  });

  it("同一请求内两次上传使用不同幂等键，协调中 20008/409 抛 ApiError", async () => {
    FakeXHR.enqueue(409, envelope(20008, null, "请求与当前资源状态冲突"));
    const err = await uploadDocuments("kb-1", [file]).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe(20008);
    expect((err as ApiError).status).toBe(409);
  });

  it("上传进度回调按 xhr.upload.onprogress 触发", async () => {
    FakeXHR.enqueue(202, envelope(0, { documents: [DOC] }));
    const progress: number[] = [];
    await uploadDocuments("kb-1", [file], {
      onProgress: (loaded, total) => progress.push(loaded / total),
    });
    expect(progress).toContain(0.4);
  });
});

describe("API 客户端：分页与限流", () => {
  it("列表携带 page/page_size 查询参数并返回分页形状", async () => {
    fetchMock.mockResolvedValue(
      fetchResponse(200, envelope(0, { items: [KB], page: 2, page_size: 10, total: 21 }))
    );
    const page = await listKnowledgeBases(2, 10);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/v1/knowledge-bases?page=2&page_size=10",
      expect.objectContaining({ method: "GET" })
    );
    expect(page).toEqual({ items: [KB], page: 2, page_size: 10, total: 21 });
  });

  it("资料列表透传公开 status 过滤", async () => {
    fetchMock.mockResolvedValue(
      fetchResponse(200, envelope(0, { items: [DOC], page: 1, page_size: 20, total: 1 }))
    );
    await listDocuments("kb-1", 1, 20, "completed");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/v1/knowledge-bases/kb-1/documents?page=1&page_size=20&status=completed",
      expect.objectContaining({ method: "GET" })
    );
  });

  it("限流 10005/429 携带 Retry-After", async () => {
    fetchMock.mockResolvedValue(
      fetchResponse(429, envelope(10005, null, "请求过于频繁，请稍后再试"), { "Retry-After": "30" })
    );
    const err = await listKnowledgeBases().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe(10005);
    expect((err as ApiError).status).toBe(429);
    expect((err as ApiError).retryAfter).toBe(30);
    expect((err as ApiError).traceId).toBe(TEST_TRACE_ID);
  });

  it("跨租户知识库 20002/404 与其他资源 20007/404 原样透传", async () => {
    fetchMock.mockResolvedValue(fetchResponse(404, envelope(20002, null, "请求的知识库不存在")));
    const err = await getDocument("kb-other", "d1").catch((e: unknown) => e);
    expect((err as ApiError).code).toBe(20002);
    fetchMock.mockResolvedValue(fetchResponse(404, envelope(20007, null, "请求的资源不存在")));
    const err2 = await deleteDocument("kb-1", "d-other").catch((e: unknown) => e);
    expect((err2 as ApiError).code).toBe(20007);
  });
});

describe("API 客户端：会话恢复与刷新轮换", () => {
  it("10001/401 时自动刷新一次并用新令牌重放原请求", async () => {
    setSession({ accessToken: "at-expired", refreshToken: "rt-1", expiresAt: Date.now() - 1000 });
    fetchMock
      .mockResolvedValueOnce(fetchResponse(401, envelope(10001, null, "请重新登录")))
      .mockResolvedValueOnce(
        fetchResponse(
          200,
          envelope(0, {
            access_token: "at-new",
            refresh_token: "rt-2",
            token_type: "Bearer",
            expires_in: 7200,
          })
        )
      )
      .mockResolvedValueOnce(fetchResponse(200, envelope(0, USER)));
    await expect(getMe()).resolves.toEqual(USER);
    // 刷新请求体携带旧 refresh token，不带 Bearer
    const refreshCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/auth/sessions")
    );
    expect(refreshCall).toBeDefined();
    const init = refreshCall![1] as RequestInit;
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ refresh_token: "rt-1" });
    // 重放请求使用新 access token
    const retryInit = fetchMock.mock.calls[2]?.[1] as RequestInit;
    expect((retryInit.headers as Record<string, string>).Authorization).toBe("Bearer at-new");
    // 会话已轮换
    expect(getSession()?.accessToken).toBe("at-new");
    expect(getSession()?.refreshToken).toBe("rt-2");
  });

  it("刷新失败（10006）清除会话并抛出原错误", async () => {
    setSession({ accessToken: "at-expired", refreshToken: "rt-1", expiresAt: Date.now() - 1000 });
    fetchMock
      .mockResolvedValueOnce(fetchResponse(401, envelope(10001, null, "请重新登录")))
      .mockResolvedValueOnce(
        fetchResponse(401, envelope(10006, null, "登录状态已失效，请重新登录"))
      );
    const err = await getMe().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(getSession()).toBeNull();
  });

  it("无会话或 10004 登录失败不触发刷新", async () => {
    fetchMock.mockResolvedValue(fetchResponse(401, envelope(10004, null, "邮箱或密码错误")));
    await login({ email: "a@example.com", password: "bad" }).catch(() => undefined);
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/auth/sessions")).length
    ).toBe(1);
  });

  it("登出携带 Bearer 与 refresh token 请求体并清除本地会话", async () => {
    setSession({ accessToken: "at-1", refreshToken: "rt_abc_xyz", expiresAt: Date.now() + 100000 });
    fetchMock.mockResolvedValue(fetchResponse(200, envelope(0, null)));
    await logout();
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/auth/sessions"));
    const init = call![1] as RequestInit;
    expect(init.method).toBe("DELETE");
    expect(JSON.parse(String(init.body))).toEqual({ refresh_token: "rt_abc_xyz" });
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer at-1");
    expect(getSession()).toBeNull();
  });

  it("登出遇到 10001 后使用轮换后的整组令牌撤销新会话", async () => {
    setSession({ accessToken: "at-expired", refreshToken: "rt-old", expiresAt: Date.now() - 1000 });
    fetchMock
      .mockResolvedValueOnce(fetchResponse(401, envelope(10001, null, "登录状态已过期")))
      .mockResolvedValueOnce(
        fetchResponse(
          200,
          envelope(0, {
            access_token: "at-new",
            refresh_token: "rt-new",
            token_type: "Bearer",
            expires_in: 7200,
          })
        )
      )
      .mockResolvedValueOnce(fetchResponse(200, envelope(0, null)));

    await logout();

    const revokeRetry = fetchMock.mock.calls[2]?.[1] as RequestInit;
    expect(revokeRetry.method).toBe("DELETE");
    expect((revokeRetry.headers as Record<string, string>).Authorization).toBe("Bearer at-new");
    expect(JSON.parse(String(revokeRetry.body))).toEqual({ refresh_token: "rt-new" });
    expect(getSession()).toBeNull();
  });
});

describe("API 客户端：SSE 帧解析（T108 封装）", () => {
  function sseStream(frames: string): ReadableStream<Uint8Array> {
    return new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(frames));
        controller.close();
      },
    });
  }

  it("按 event/data 文本帧解码判别事件并解析信封", async () => {
    const frames = [
      `event: message_start\ndata: ${JSON.stringify(envelope(0, { message_id: "m1" }))}\n\n`,
      `event: delta\ndata: ${JSON.stringify(envelope(0, { text: "根据" }))}\n\n`,
      `event: message_end\ndata: ${JSON.stringify(envelope(0, { message_id: "m1", finish_reason: "stop" }))}\n\n`,
    ].join("");
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      body: sseStream(frames),
      json: async () => ({}),
    } as Response);
    const events: { event: string; data: { code: number } }[] = [];
    await streamEvents("/conversations/c1/messages", {
      onEvent: (event, data) => events.push({ event, data }),
    });
    expect(events.map((e) => e.event)).toEqual(["message_start", "delta", "message_end"]);
    expect(events[1]?.data).toMatchObject({ code: 0 });
  });

  it("SSE 请求携带 Bearer 会话", async () => {
    setSession({ accessToken: "at-sse", refreshToken: "rt-1", expiresAt: Date.now() + 100000 });
    fetchMock.mockResolvedValue({ ok: true, status: 200, body: sseStream("") } as Response);
    await streamEvents("/conversations/c1/messages", { onEvent: () => undefined });
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer at-sse");
  });
});

describe("API 客户端：知识库与资料变更", () => {
  it("创建知识库 POST /knowledge-bases", async () => {
    fetchMock.mockResolvedValue(fetchResponse(201, envelope(0, KB)));
    await expect(createKnowledgeBase({ name: "笔记" })).resolves.toEqual(KB);
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ name: "笔记" });
  });

  it("更新本人基本资料 PATCH /users/me", async () => {
    fetchMock.mockResolvedValue(fetchResponse(200, envelope(0, { ...USER, display_name: "A" })));
    await expect(updateMe({ display_name: "A" })).resolves.toMatchObject({ display_name: "A" });
  });

  it("删除知识库与资料返回 void", async () => {
    fetchMock.mockResolvedValue(fetchResponse(200, envelope(0, null)));
    await expect(deleteKnowledgeBase("kb-1")).resolves.toBeUndefined();
    await expect(deleteDocument("kb-1", "d1")).resolves.toBeUndefined();
    const methods = fetchMock.mock.calls.map(([, init]) => (init as RequestInit).method);
    expect(methods).toEqual(["DELETE", "DELETE"]);
  });
});

describe("API 客户端：注册", () => {
  it("注册成功返回用户（不带会话）", async () => {
    fetchMock.mockResolvedValue(fetchResponse(201, envelope(0, USER)));
    await expect(
      register({ email: "a@example.com", password: "pw-123456", display_name: "Alice" })
    ).resolves.toEqual(USER);
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      email: "a@example.com",
      password: "pw-123456",
      display_name: "Alice",
    });
  });

  it("邮箱已注册 20006/409 原样透传", async () => {
    fetchMock.mockResolvedValue(
      fetchResponse(409, envelope(20006, null, "该邮箱已注册，请直接登录"))
    );
    const err = await register({ email: "a@example.com", password: "pw-123456" }).catch(
      (e: unknown) => e
    );
    expect((err as ApiError).code).toBe(20006);
  });
});
