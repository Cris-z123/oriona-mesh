import { Badge } from "@/components/ui/badge";
import { isTombstone, statusLabel } from "@/features/documents/status";
import type { Document } from "@/lib/api/types";

/** 资料状态仅映射服务端 DTO，不在页面内重复状态标签与墓碑判断。 */
export function DocumentStatus({ document }: { document: Document }) {
  return (
    <Badge variant={isTombstone(document) ? "destructive" : "secondary"}>
      {statusLabel(document.status)}
    </Badge>
  );
}
