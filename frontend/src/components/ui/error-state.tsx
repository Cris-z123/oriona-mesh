import { AlertCircle } from "lucide-react";

import type { ApiError } from "@/lib/api/client";

/**
 * 统一可恢复错误提示（ui-design §5/§6.2）：展示服务端 msg 与可复制 trace_id，
 * 不展示原始响应、令牌或堆栈；状态同时通过图标与文字表达。
 */
export function ErrorState({ error }: { error: ApiError }) {
  return (
    <div
      role="alert"
      className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
    >
      <div className="flex items-start gap-2">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
        <div className="min-w-0">
          <p>{error.msg}</p>
          {error.traceId ? (
            <p className="mt-0.5 text-xs opacity-70">trace_id: {error.traceId}</p>
          ) : null}
        </div>
      </div>
    </div>
  );
}
