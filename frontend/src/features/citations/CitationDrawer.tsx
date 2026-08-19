"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { listCitations } from "@/lib/api/client";
import { queryKeys } from "@/lib/query-keys";
import { useUiStore } from "@/stores/ui-store";

interface CitationSelection {
  messageId: string;
  rank: number;
}

function parseCitationSelection(selector: string | null): CitationSelection | null {
  if (!selector) return null;
  const [prefix, messageId, rawRank, ...rest] = selector.split(":");
  const rank = Number(rawRank);
  if (
    prefix !== "citation" ||
    !messageId ||
    rest.length > 0 ||
    !Number.isInteger(rank) ||
    rank < 1
  ) {
    return null;
  }
  return { messageId, rank };
}

/**
 * 引用抽屉只展示 API 的 Citation DTO。snapshot 没有 document/chunk ID，绝不生成原始资料入口。
 */
export function CitationDrawer({
  conversationId,
  knowledgeBaseId,
}: {
  conversationId: string;
  knowledgeBaseId?: string;
}) {
  const selector = useUiStore((state) => state.citationDrawerSelector);
  const close = useUiStore((state) => state.closeCitationDrawer);
  const citationSelection = parseCitationSelection(selector);
  const citations = useInfiniteQuery({
    queryKey: queryKeys.citationsAll(conversationId, citationSelection?.messageId ?? ""),
    initialPageParam: 1,
    queryFn: ({ pageParam }) => {
      if (!citationSelection) throw new Error("缺少引用消息标识");
      return listCitations(conversationId, citationSelection.messageId, pageParam, 20);
    },
    enabled: citationSelection !== null,
    getNextPageParam: (lastPage) =>
      lastPage.page * lastPage.page_size < lastPage.total ? lastPage.page + 1 : undefined,
  });
  const citation = citations.data?.pages
    .flatMap((page) => page.items)
    .find((item) => item.rank === citationSelection?.rank);

  useEffect(() => {
    if (citationSelection && !citation && citations.hasNextPage && !citations.isFetchingNextPage) {
      void citations.fetchNextPage();
    }
  }, [citation, citationSelection, citations]);

  return (
    <Sheet open={citationSelection !== null} onOpenChange={(open) => !open && close()}>
      <SheetContent side="right" aria-label="引用详情">
        <SheetTitle>引用详情</SheetTitle>
        {citation ? (
          <div className="space-y-3 text-sm">
            <p className="font-medium">
              [{citation.rank}] {citation.filename}
            </p>
            <SheetDescription>
              {citation.source_type === "snapshot" ? "来源快照" : "当前来源"}
              {citation.page ? ` · 第 ${citation.page} 页` : ""}
              {citation.section ? ` · ${citation.section}` : ""}
            </SheetDescription>
            <blockquote className="border-l-2 border-primary/50 pl-3 text-muted-foreground">
              {citation.content}
            </blockquote>
            {citation.source_type === "live" && citation.document_id && knowledgeBaseId ? (
              <a
                className="inline-flex text-sm font-medium text-primary underline-offset-4 hover:underline"
                href={`/knowledge-bases/${knowledgeBaseId}?document=${citation.document_id}`}
              >
                定位到资料
              </a>
            ) : null}
          </div>
        ) : citations.isError ? (
          <div className="space-y-2">
            <SheetDescription>引用详情加载失败，请检查网络后重试。</SheetDescription>
            <button
              type="button"
              className="text-sm font-medium text-primary underline-offset-4 hover:underline"
              onClick={() => void citations.refetch()}
            >
              重试
            </button>
          </div>
        ) : citations.hasNextPage ? (
          <SheetDescription>正在加载引用详情…</SheetDescription>
        ) : (
          <SheetDescription>未找到该引用。</SheetDescription>
        )}
      </SheetContent>
    </Sheet>
  );
}
