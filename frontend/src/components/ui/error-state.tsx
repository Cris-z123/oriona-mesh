"use client";

import { AlertCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
export interface ErrorStateValue {
  msg: string;
  traceId: string | null;
}

/** 将未知请求异常收敛为安全展示值；存在服务端 msg 时始终优先使用。 */
export function toErrorStateValue(error: unknown, fallbackMessage: string): ErrorStateValue {
  const value = typeof error === "object" && error !== null ? error : {};
  const record = value as Record<string, unknown>;
  return {
    msg: typeof record.msg === "string" ? record.msg : fallbackMessage,
    traceId: typeof record.traceId === "string" ? record.traceId : null,
  };
}

/** 复制反馈展示时长（毫秒），避免“已复制追踪 ID”常驻。 */
const COPIED_RESET_MS = 2_000;

/**
 * 统一可恢复错误提示（ui-design §5/§6.2）：展示服务端 msg 与可复制 trace_id，
 * 不展示原始响应、令牌或堆栈；状态同时通过图标与文字表达。
 */
export function ErrorState({ error, onRetry }: { error: ErrorStateValue; onRetry?: () => void }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), COPIED_RESET_MS);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const copyTraceId = async () => {
    if (!error.traceId || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(error.traceId);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div
      role="alert"
      className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
    >
      <div className="flex items-start gap-2">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
        <div className="min-w-0">
          <p>{error.msg}</p>
          {onRetry ? (
            <Button variant="outline" className="mt-2 h-8" onClick={onRetry}>
              重试
            </Button>
          ) : null}
          {error.traceId ? (
            <div className="mt-0.5 flex items-center gap-2 text-xs opacity-70">
              <span>trace_id: {error.traceId}</span>
              <Button
                variant="ghost"
                className="h-6 px-2 text-xs"
                aria-label="复制追踪 ID"
                onClick={() => void copyTraceId()}
              >
                复制
              </Button>
              {copied ? <span aria-live="polite">已复制追踪 ID</span> : null}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
