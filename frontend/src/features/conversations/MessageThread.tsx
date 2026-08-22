"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { Send, Square } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import { ErrorState, toErrorStateValue } from "@/components/ui/error-state";
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

  if (citations.isError) {
    return (
      <ErrorState
        error={toErrorStateValue(citations.error, "引用加载失败。")}
        onRetry={() => void citations.refetch()}
      />
    );
  }
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

/** 乐观用户消息：终态后由持久化消息查询接管（T159）。 */
function pendingUserMessage(conversationId: string, content: string): Message {
  return {
    id: `pending-${conversationId}`,
    conversation_id: conversationId,
    role: "user",
    content,
    status: "completed",
    rewritten_query: null,
    finish_reason: null,
    created_at: new Date().toISOString(),
  };
}

/**
 * 消息历史由 Query 管理；仅当前一次 SSE 的草稿驻留在 hook 生命周期。
 * 发送后立即以乐观用户消息呈现，终态后失效持久化消息与引用查询接管（T159）。
 * 消息区维护底部跟随：用户停留在底部时自动滚动，上翻阅读历史时提供回到最新入口。
 */
export function MessageThread({
  conversationId,
  initialDraft,
}: {
  conversationId: string;
  /** 空态输入即新建会话时，创建完成后自动发送的首条内容（仅消费一次）。 */
  initialDraft?: string;
}) {
  const [content, setContent] = useState("");
  const [pendingUser, setPendingUser] = useState<string | null>(null);
  const { cancel, feedback, isStreaming, send, streamCitations, streamMessage } =
    useMessageStream(conversationId);
  const draftHandled = useRef(false);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const messages = useInfiniteQuery({
    queryKey: queryKeys.messagesAll(conversationId),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => listMessages(conversationId, pageParam, 50),
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? (lastPage.next_before ?? undefined) : undefined,
  });

  useEffect(() => {
    if (!initialDraft || draftHandled.current) return;
    draftHandled.current = true;
    void send(initialDraft);
  }, [initialDraft, send]);

  const submit = async () => {
    const question = content.trim();
    if (!question || isStreaming) return;
    setContent("");
    // 乐观呈现用户消息：终态后 refetch 的持久化消息会替换它。
    setPendingUser(question);
    try {
      await send(question);
    } catch {
      setPendingUser((current) => (current === question ? null : current));
    }
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter") return;
    // 中文等输入法用 Enter 确认候选词：不得打断 composition 或插入换行。
    if (event.nativeEvent.isComposing) return;
    if (event.ctrlKey || event.metaKey) {
      event.preventDefault();
      void submit();
      return;
    }
    event.preventDefault();
    setContent((current) => `${current}\n`.slice(0, 12_000));
  };

  // 服务端每页按 (created_at, id) 倒序返回；页面按时间正序展示，旧页前插。
  const history =
    messages.data?.pages
      .slice()
      .reverse()
      .flatMap((page) => [...page.items].reverse()) ?? [];
  const persistedUserMessages = new Set(
    history.filter((message) => message.role === "user").map((message) => message.content)
  );
  const visibleMessages: Message[] = [
    ...history,
    ...(pendingUser && !persistedUserMessages.has(pendingUser)
      ? [pendingUserMessage(conversationId, pendingUser)]
      : []),
    ...(streamMessage ? [streamMessage] : []),
  ];

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
  };
  // 新内容到达时：用户停留在底部则自动跟随；阅读历史时不打断。
  useEffect(() => {
    const el = scrollerRef.current;
    if (el && atBottom) el.scrollTop = el.scrollHeight;
  }, [visibleMessages.length, streamMessage?.content, atBottom]);
  const scrollToLatest = () => {
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setAtBottom(true);
  };

  return (
    <section className="flex min-h-0 flex-1 flex-col gap-4" aria-label="消息线程">
      {messages.isLoading ? (
        <div aria-label="正在加载消息" className="text-sm text-muted-foreground">
          正在加载消息…
        </div>
      ) : null}
      {/* 消息区独立滚动（T151）；维护底部跟随（T159）。 */}
      <div
        ref={scrollerRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 space-y-3 overflow-y-auto"
        aria-label="消息历史"
      >
        {visibleMessages.map((message) => {
          // 标准 AI 单列消息流（T159）：用户右侧紧凑气泡，助手左侧无边框正文。
          const isUser = message.role === "user";
          const isStreaming =
            message.id === streamMessage?.id && streamMessage?.status === "streaming";
          return (
            <article
              key={message.id}
              aria-label={isUser ? "你" : "Oriona"}
              className={isUser ? "flex justify-end" : "flex justify-start"}
            >
              {isUser ? (
                <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-primary px-4 py-2 text-sm text-primary-foreground">
                  {message.content}
                </div>
              ) : (
                <div className="max-w-[85%] text-sm">
                  <div className="whitespace-pre-wrap">
                    {message.content}
                    {isStreaming ? (
                      <span
                        aria-hidden
                        className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-primary align-text-bottom"
                      />
                    ) : null}
                  </div>
                  {message.id === streamMessage?.id && streamCitations.length > 0 ? (
                    <CitationCard messageId={message.id} citations={streamCitations} />
                  ) : null}
                  {message.id !== streamMessage?.id ? (
                    <HistoricalCitationCard
                      conversationId={conversationId}
                      messageId={message.id}
                    />
                  ) : null}
                </div>
              )}
            </article>
          );
        })}
        {!atBottom ? (
          <div className="sticky bottom-2 flex justify-center">
            <Button
              variant="outline"
              className="h-8 px-3 text-xs shadow-sm"
              onClick={scrollToLatest}
            >
              回到最新内容
            </Button>
          </div>
        ) : null}
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
        <div className="space-y-2">
          <ConversationFeedback {...feedbackFromError(messages.error)} />
          <Button variant="outline" onClick={() => void messages.refetch()}>
            重试
          </Button>
        </div>
      ) : null}
      {/* 输入区固定在主内容区底部（T151）：圆角卡片悬浮在消息区下方，
          快捷键提示在卡片内，发送/取消为右下角小图标按钮。 */}
      <div className="shrink-0 rounded-xl border bg-surface p-2 shadow-sm">
        <label className="sr-only" htmlFor="message-content">
          输入问题
        </label>
        <textarea
          id="message-content"
          className="min-h-24 w-full resize-none bg-transparent px-2 py-1 pr-10 text-sm outline-none focus:outline-none focus:ring-0"
          value={content}
          onChange={(event) => setContent(event.target.value.slice(0, 12_000))}
          onKeyDown={onKeyDown}
          placeholder="继续追问，或在已有资料中检索…"
        />
        <div className="flex items-center justify-between px-1 pt-1">
          <span className="text-xs text-muted-foreground">
            Enter 换行 · Ctrl/Cmd+Enter 发送 · 上限 12,000 字
          </span>
          {isStreaming ? (
            <Button
              variant="outline"
              className="h-8 w-8 rounded-full p-0"
              aria-label="取消"
              onClick={cancel}
            >
              <Square className="h-4 w-4" aria-hidden />
            </Button>
          ) : (
            <Button
              className="h-8 w-8 rounded-full p-0"
              aria-label="发送"
              disabled={!content.trim()}
              onClick={() => void submit()}
            >
              <Send className="h-4 w-4" aria-hidden />
            </Button>
          )}
        </div>
      </div>
    </section>
  );
}
