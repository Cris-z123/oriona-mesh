"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { CitationCard } from "@/features/citations/CitationCard";
import { listCitations, listMessages } from "@/lib/api/client";
import type { Message } from "@/lib/api/types";
import { queryKeys } from "@/lib/query-keys";

import { ConversationFeedback } from "./ConversationFeedback";
import { feedbackFromError, useMessageStream } from "./useMessageStream";

/** 已持久化回答的引用单独查询，避免把引用实体复制进客户端 store。 */
function HistoricalCitationCard({
  conversationId,
  messageId,
}: {
  conversationId: string;
  messageId: string;
}) {
  const citations = useInfiniteQuery({
    queryKey: queryKeys.citationsAll(conversationId, messageId),
    initialPageParam: 1,
    queryFn: ({ pageParam }) => listCitations(conversationId, messageId, pageParam, 20),
    getNextPageParam: (lastPage) =>
      lastPage.page * lastPage.page_size < lastPage.total ? lastPage.page + 1 : undefined,
  });
  const items = citations.data?.pages.flatMap((page) => page.items) ?? [];

  if (!items.length) return null;
  return (
    <div className="space-y-2">
      <CitationCard messageId={messageId} citations={items} />
      {citations.hasNextPage ? (
        <Button
          variant="ghost"
          className="h-8 px-2 text-xs"
          disabled={citations.isFetchingNextPage}
          onClick={() => void citations.fetchNextPage()}
        >
          {citations.isFetchingNextPage ? "正在加载引用…" : "加载更多引用"}
        </Button>
      ) : null}
    </div>
  );
}

/**
 * 消息历史由 Query 管理；仅当前一次 SSE 的草稿驻留在 hook 生命周期。
 * 终态后失效持久化消息与引用查询，确保服务端记录重新成为 UI 真相。
 */
export function MessageThread({ conversationId }: { conversationId: string }) {
  const [content, setContent] = useState("");
  const { cancel, feedback, isStreaming, send, streamCitations, streamMessage } =
    useMessageStream(conversationId);
  const messages = useInfiniteQuery({
    queryKey: queryKeys.messagesAll(conversationId),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => listMessages(conversationId, pageParam, 50),
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? (lastPage.next_before ?? undefined) : undefined,
  });

  const submit = async () => {
    const question = content.trim();
    if (!question || isStreaming) return;
    setContent("");
    await send(question);
  };

  // 服务端每页按 (created_at, id) 倒序返回；页面按时间正序展示，旧页前插。
  const history =
    messages.data?.pages
      .slice()
      .reverse()
      .flatMap((page) => [...page.items].reverse()) ?? [];
  const visibleMessages: Message[] = streamMessage ? [...history, streamMessage] : history;

  return (
    <section className="space-y-4" aria-label="消息线程">
      <div className="space-y-3">
        {visibleMessages.map((message) => (
          <article key={message.id} className="rounded-md border bg-surface p-3 text-sm">
            <p className="mb-1 text-xs text-muted-foreground">
              {message.role === "user" ? "你" : "Oriona"}
            </p>
            <p>{message.content || "正在生成…"}</p>
            {message.role === "assistant" &&
            message.id === streamMessage?.id &&
            streamCitations.length > 0 ? (
              <CitationCard messageId={message.id} citations={streamCitations} />
            ) : null}
            {message.role === "assistant" && message.id !== streamMessage?.id ? (
              <HistoricalCitationCard conversationId={conversationId} messageId={message.id} />
            ) : null}
          </article>
        ))}
      </div>
      {messages.hasNextPage ? (
        <Button
          variant="ghost"
          onClick={() => void messages.fetchNextPage()}
          disabled={messages.isFetchingNextPage}
        >
          {messages.isFetchingNextPage ? "正在加载…" : "加载更早消息"}
        </Button>
      ) : null}
      {feedback ? <ConversationFeedback {...feedback} /> : null}
      {messages.isError && !feedback ? (
        <ConversationFeedback {...feedbackFromError(messages.error)} />
      ) : null}
      <div className="flex gap-2">
        <label className="sr-only" htmlFor="message-content">
          输入问题
        </label>
        <textarea
          id="message-content"
          className="min-h-10 flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm"
          value={content}
          onChange={(event) => setContent(event.target.value)}
          placeholder="继续追问，或在已有资料中检索…"
        />
        {isStreaming ? (
          <Button variant="outline" onClick={cancel}>
            取消
          </Button>
        ) : (
          <Button onClick={() => void submit()} disabled={!content.trim()}>
            发送
          </Button>
        )}
      </div>
    </section>
  );
}
