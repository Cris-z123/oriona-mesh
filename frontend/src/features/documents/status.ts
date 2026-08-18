import type { DocumentStatus } from "@/lib/api/types";

/** 公开资料状态的中文标签（服务端枚举，客户端只做展示映射）。 */
export function statusLabel(status: DocumentStatus): string {
  switch (status) {
    case "pending":
      return "待处理";
    case "queued":
      return "排队中";
    case "processing":
      return "处理中";
    case "completed":
      return "已完成";
    case "failed":
      return "失败";
  }
}

/** 是否仍在处理中（需要轮询）。 */
export function isInFlight(status: DocumentStatus): boolean {
  return status === "pending" || status === "queued" || status === "processing";
}
