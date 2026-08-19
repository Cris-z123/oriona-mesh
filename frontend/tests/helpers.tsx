import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";

/**
 * 前端测试共享助手（T134/T135 起）。
 * 组件测试统一使用禁用重试的 QueryClient，保证 API mock 调用次数确定；
 * 生产默认值（retry/refetchOnWindowFocus/staleTime）由 `@/lib/query-client` 的
 * `makeQueryClient` 单独验证，此处不做复制。
 */
export function makeTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

/** 渲染带独立测试 QueryClient 的组件。 */
export function renderWithProviders(ui: ReactElement): RenderResult {
  return render(<QueryClientProvider client={makeTestQueryClient()}>{ui}</QueryClientProvider>);
}
