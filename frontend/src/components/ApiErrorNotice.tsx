import type { ApiError } from "@/lib/api/client";

/**
 * 统一业务错误提示（FR-021）：展示服务端返回的 msg 与 trace_id，
 * 便于用户反馈时提供可追踪标识；不复制任何业务错误码判断。
 */
export function ApiErrorNotice({ error }: { error: ApiError }) {
  return (
    <div
      role="alert"
      className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
    >
      <p>{error.msg}</p>
      {error.traceId ? (
        <p className="mt-0.5 text-xs opacity-70">trace_id: {error.traceId}</p>
      ) : null}
    </div>
  );
}
