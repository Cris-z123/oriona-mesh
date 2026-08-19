"use client";

import { QueryClient, QueryClientProvider, type QueryClientConfig } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";

import { subscribeSession } from "@/lib/api/session";

/**
 * 统一 Query 配置与 Provider（T136/T138，ui-design §6）。
 *
 * - TanStack Query 是唯一的 REST 服务器状态缓存层；查询键按资源层级构造；
 * - mutation 默认不自动重试非幂等写请求；查询有限重试（1 次）并关闭窗口聚焦重取；
 * - 会话清空（登出）时清空整个缓存，Query 缓存不得跨用户会话复用（ui-design §6.2）；
 * - 资料非终态轮询由各 feature 查询封装的 refetchInterval 依 DTO 决定。
 */

const QUERY_STALE_TIME_MS = 30_000;

export function makeQueryClient(config: QueryClientConfig = {}): QueryClient {
  const { defaultOptions, ...rest } = config;
  return new QueryClient({
    ...rest,
    defaultOptions: {
      queries: {
        retry: 1,
        refetchOnWindowFocus: false,
        staleTime: QUERY_STALE_TIME_MS,
        ...defaultOptions?.queries,
      },
      mutations: {
        retry: false,
        ...defaultOptions?.mutations,
      },
    },
  });
}

/** 会话清空时清空缓存；返回解除订阅函数（QueryProvider 与测试复用）。 */
export function bindQueryClientToSession(client: QueryClient): () => void {
  return subscribeSession((session) => {
    if (!session) client.clear();
  });
}

export function QueryProvider({ children }: { children: ReactNode }) {
  const [client] = useState(() => makeQueryClient());

  useEffect(() => bindQueryClientToSession(client), [client]);

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
