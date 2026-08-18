import type { ApiError } from "@/lib/api/client";

import { ErrorState } from "@/components/ui/error-state";

/**
 * 统一业务错误提示（FR-021）：委托给 `ErrorState` 呈现服务端 msg 与 trace_id。
 * 保留本组件以兼容阶段 8 既有调用方；新组件直接使用 `ErrorState`。
 */
export function ApiErrorNotice({ error }: { error: ApiError }) {
  return <ErrorState error={error} />;
}
