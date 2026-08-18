import { QueryClient, QueryClientProvider, type QueryClientConfig } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  queryKeys,
  useDeleteDocument,
  useDocumentDetail,
  useDocumentList,
} from "@/features/documents/queries";
import { clearSession, setSession } from "@/lib/api/session";
import { bindQueryClientToSession, makeQueryClient } from "@/lib/query-client";
import type { Document } from "@/lib/api/types";

/**
 * T135 [P] Query Provider 单元测试（先写后验）。
 *
 * 覆盖（ui-design §6.1/6.2）：
 * - makeQueryClient 默认策略：mutation 不自动重试非幂等写请求、查询有限重试、关闭窗口聚焦重取；
 * - 登出（会话清空）清空整个 Query 缓存，禁止跨用户会话复用；
 * - 精确失效：删除资料只标记该知识库的资料子树过期，不影响其他知识库或知识库列表；
 * - DTO 驱动的非终态资料轮询：非终态时按间隔重取，全部终态后停止。
 */
const api = vi.hoisted(() => ({
  listDocuments: vi.fn(),
  getDocument: vi.fn(),
  deleteDocument: vi.fn(),
}));

vi.mock("@/lib/api/client", () => api);

function doc(overrides: Partial<Document> = {}): Document {
  return {
    id: "d1",
    knowledge_base_id: "kb-1",
    filename: "a.txt",
    file_type: "txt",
    file_size: 10,
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
    ...overrides,
  };
}

const page = (items: Document[], total = items.length) => ({
  items,
  page: 1,
  page_size: 20,
  total,
});

function makeTestClient(config?: QueryClientConfig): QueryClient {
  return makeQueryClient({
    ...config,
    defaultOptions: {
      queries: { retry: false, ...config?.defaultOptions?.queries },
      mutations: { retry: false, ...config?.defaultOptions?.mutations },
    },
  });
}

function wrapper(client: QueryClient) {
  // 本文件为 .ts（非 .tsx）：以 createElement 组装 Provider
  return function TestWrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children);
  };
}

beforeEach(() => {
  clearSession();
  for (const fn of Object.values(api)) {
    (fn as ReturnType<typeof vi.fn>).mockReset();
  }
});

afterEach(() => {
  clearSession();
});

describe("makeQueryClient 默认策略", () => {
  it("mutation 不自动重试非幂等写请求；查询有限重试且关闭窗口聚焦重取", () => {
    const client = makeQueryClient();
    const options = client.getDefaultOptions();
    expect(options.mutations?.retry).toBe(false);
    expect(options.queries?.retry).toBe(1);
    expect(options.queries?.refetchOnWindowFocus).toBe(false);
    expect(options.queries?.staleTime).toBe(30_000);
  });
});

describe("登出清空缓存（ui-design §6.2）", () => {
  it("会话清空时清空整个 Query 缓存；登录/刷新不清空", () => {
    const client = makeTestClient();
    const unbind = bindQueryClientToSession(client);
    client.setQueryData(["knowledgeBases"], { items: [] });
    expect(client.getQueryData(["knowledgeBases"])).toBeDefined();

    // 会话存在（登录/刷新）不清空缓存
    setSession({ accessToken: "at-1", refreshToken: "rt_1", expiresAt: Date.now() + 100_000 });
    expect(client.getQueryData(["knowledgeBases"])).toBeDefined();

    // 登出（会话清空）后缓存整体清空
    clearSession();
    expect(client.getQueryData(["knowledgeBases"])).toBeUndefined();
    expect(client.getQueryCache().findAll()).toHaveLength(0);
    unbind();
  });
});

describe("删除资料的精确失效", () => {
  it("只标记该知识库的资料子树（列表与详情）过期，不影响其他知识库与知识库列表", async () => {
    api.deleteDocument.mockResolvedValue(undefined);
    const client = makeTestClient();
    client.setQueryData(queryKeys.documentList("kb-1", 1, 20, "all"), page([doc()]));
    client.setQueryData(queryKeys.documentDetail("kb-1", "d1"), doc());
    client.setQueryData(queryKeys.documentList("kb-2", 1, 20, "all"), page([doc({ id: "d9" })]));
    client.setQueryData(queryKeys.knowledgeBases(1, 20), page([]));

    const { result } = renderHook(() => useDeleteDocument(), {
      wrapper: wrapper(client),
    });
    await act(async () => {
      await result.current.mutateAsync({ knowledgeBaseId: "kb-1", documentId: "d1" });
    });

    // 失效以 QueryState.isInvalidated 表达（RQ v5 状态中无 isStale 字段）
    expect(client.getQueryState(queryKeys.documentList("kb-1", 1, 20, "all"))?.isInvalidated).toBe(
      true
    );
    expect(client.getQueryState(queryKeys.documentDetail("kb-1", "d1"))?.isInvalidated).toBe(true);
    // 精确边界：其他知识库与知识库列表不受影响
    expect(
      client.getQueryState(queryKeys.documentList("kb-2", 1, 20, "all"))?.isInvalidated
    ).toBeFalsy();
    expect(client.getQueryState(queryKeys.knowledgeBases(1, 20))?.isInvalidated).toBeFalsy();
  });
});

describe("DTO 驱动的非终态资料轮询", () => {
  it("列表存在非终态资料时按间隔重取，全部终态后停止", async () => {
    api.listDocuments
      .mockResolvedValueOnce(page([doc({ status: "processing" })]))
      .mockResolvedValueOnce(page([doc({ status: "completed" })]));
    const { result } = renderHook(
      () => useDocumentList("kb-1", 1, 20, "all", { pollIntervalMs: 50 }),
      { wrapper: wrapper(makeTestClient()) }
    );
    await waitFor(() => expect(api.listDocuments).toHaveBeenCalledTimes(1));
    // 非终态触发第二次轮询
    await waitFor(() => expect(api.listDocuments).toHaveBeenCalledTimes(2));
    expect(result.current.data?.items[0]?.status).toBe("completed");
    // 全部终态后不再轮询
    await new Promise((r) => setTimeout(r, 150));
    expect(api.listDocuments).toHaveBeenCalledTimes(2);
  });

  it("详情 DTO 非终态时轮询，终态后停止", async () => {
    api.getDocument
      .mockResolvedValueOnce(doc({ id: "d1", status: "queued" }))
      .mockResolvedValueOnce(doc({ id: "d1", status: "completed" }));
    const { result } = renderHook(() => useDocumentDetail("kb-1", "d1", { pollIntervalMs: 50 }), {
      wrapper: wrapper(makeTestClient()),
    });
    await waitFor(() => expect(api.getDocument).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.getDocument).toHaveBeenCalledTimes(2));
    expect(result.current.data?.status).toBe("completed");
    await new Promise((r) => setTimeout(r, 150));
    expect(api.getDocument).toHaveBeenCalledTimes(2);
  });
});
