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
    <ol className="mt-3 space-y-1 border-l-2 border-clue/50 pl-3" aria-label="回答引用">
      {[...citations]
        .sort((left, right) => left.rank - right.rank)
        .map((citation) => (
          <li key={citation.rank} className="relative">
            <button
              type="button"
              className="w-full py-2 text-left text-xs text-clue focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label={`查看引用 ${citation.filename}`}
              onClick={() => open(`citation:${messageId}:${citation.rank}`)}
            >
              <span className="flex items-center gap-2 font-medium">
                <span aria-label={`引用序号 ${citation.rank}`}>[{citation.rank}]</span>
                <span>{citation.filename}</span>
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
