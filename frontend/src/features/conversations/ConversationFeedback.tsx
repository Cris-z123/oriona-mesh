"use client";

import { CircleX, SearchX } from "lucide-react";

import { ErrorState } from "@/components/ui/error-state";

export type ConversationFeedbackKind = "no_evidence" | "failed" | "cancelled";

/** 会话终态反馈：文案、图标与颜色同时表达，不把取消伪装为失败。 */
export function ConversationFeedback({
  kind,
  message,
  traceId,
  code,
  status,
  retryAfter,
}: {
  kind: ConversationFeedbackKind;
  message?: string;
  traceId?: string | null;
  code?: number;
  status?: number;
  retryAfter?: number | null;
}) {
  if (kind === "no_evidence") {
    return (
      <div role="status" className="rounded-md border border-primary/25 bg-primary/5 p-3 text-sm">
        <SearchX className="mr-2 inline h-4 w-4 text-primary" aria-hidden />
        未找到相关证据。请换一种问法，或检查资料是否已完成处理。
      </div>
    );
  }

  if (kind === "cancelled") {
    return (
      <div
        role="status"
        className="rounded-md border border-muted-foreground/25 bg-muted p-3 text-sm"
      >
        <CircleX className="mr-2 inline h-4 w-4" aria-hidden />
        回答已取消。
      </div>
    );
  }

  const failureMessage =
    code === 10001
      ? "登录状态已过期，请重新登录。"
      : code === 20007 || status === 404
        ? "当前内容不存在或已无权访问。"
        : code === 10005 || status === 429
          ? retryAfter !== null && retryAfter !== undefined
            ? `请求过于频繁，请于 ${retryAfter} 秒后重试。`
            : "请求过于频繁，请稍后重试。"
          : (message ?? "回答生成失败，请稍后重试。");

  return <ErrorState error={{ msg: failureMessage, traceId: traceId ?? null }} />;
}
