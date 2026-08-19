"use client";

import type { Citation } from "@/lib/api/types";
import { useUiStore } from "@/stores/ui-store";

/** 回答下方的紧凑证据卡；rank 是契约排序字段，抽屉选择器不是服务端实体 ID。 */
export function CitationCard({
  citations,
  messageId,
}: {
  citations: Citation[];
  messageId: string;
}) {
  const open = useUiStore((state) => state.openCitationDrawer);
  return (
    <ol className="mt-3 grid gap-2 sm:grid-cols-2" aria-label="回答引用">
      {[...citations]
        .sort((left, right) => left.rank - right.rank)
        .map((citation) => (
          <li key={citation.rank}>
            <button
              type="button"
              className="w-full rounded-md border border-primary/25 bg-primary/5 p-3 text-left text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label={`查看引用 ${citation.filename}`}
              onClick={() => open(`citation:${messageId}:${citation.rank}`)}
            >
              <span className="font-medium text-primary">
                [{citation.rank}] {citation.filename}
              </span>
              <span className="mt-1 block text-muted-foreground">
                {citation.section ?? (citation.page ? `第 ${citation.page} 页` : "来源片段")}
              </span>
            </button>
          </li>
        ))}
    </ol>
  );
}
