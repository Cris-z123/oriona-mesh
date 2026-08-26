"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Database, Send } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState, type KeyboardEvent } from "react";

import { AppShell } from "@/components/app-shell/AppShell";
import { CitationDrawer } from "@/features/citations/CitationDrawer";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState, toErrorStateValue, type ErrorStateValue } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { KnowledgeBasePicker } from "@/features/knowledge-bases/KnowledgeBasePicker";
import {
  asApiError,
  createConversation,
  getConversation,
  getKnowledgeBase,
} from "@/lib/api/client";
import { queryKeys } from "@/lib/query-keys";
import { ERROR_CODES } from "@/lib/api/types";
import { useUiStore } from "@/stores/ui-store";

import { MessageThread } from "./MessageThread";

/** ui-design §6.2：会话加载错误中 20007/404 映射为固定友好文案，其余展示服务端 msg。 */
function conversationErrorValue(error: unknown): ErrorStateValue {
  const value = toErrorStateValue(error, "当前内容不存在或已无权访问。");
  const record =
    typeof error === "object" && error !== null ? (error as Record<string, unknown>) : {};
  if (record.code === ERROR_CODES.RESOURCE_NOT_FOUND || record.status === 404) {
    return { ...value, msg: "当前内容不存在或已无权访问。" };
  }
  return value;
}

/**
 * 会话主内容区（T151/T157，ui-design §3.1）：顶部当前知识库选择（按需加载全部、
 * 绝不回退），左栏会话历史位于全局侧栏，正文为消息线程；已有会话的知识库
 * 上下文只读，无会话时才显示选择器并提供“输入即新建”起始态。
 */
export function ConversationsWorkspace() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const searchParams = useSearchParams();
  const conversationId = searchParams.get("conversation");
  const knowledgeBaseId = searchParams.get("knowledgeBase") ?? "";
  const closeCitationDrawer = useUiStore((state) => state.closeCitationDrawer);
  const [starterContent, setStarterContent] = useState("");
  const [startError, setStartError] = useState<ReturnType<typeof asApiError> | null>(null);
  const [draft, setDraft] = useState<{ conversationId: string; content: string } | null>(null);

  const selectedConversation = useQuery({
    queryKey: queryKeys.conversationDetail(conversationId ?? ""),
    queryFn: () => getConversation(conversationId ?? ""),
    enabled: Boolean(conversationId),
  });
  // 正文顶栏显示绑定知识库：以会话归属知识库为准，与顶部选择器保持一致。
  const boundKnowledgeBaseId = selectedConversation.data?.knowledge_base_id ?? knowledgeBaseId;
  const boundKnowledgeBase = useQuery({
    queryKey: queryKeys.knowledgeBaseDetail(boundKnowledgeBaseId),
    queryFn: () => getKnowledgeBase(boundKnowledgeBaseId),
    enabled: Boolean(boundKnowledgeBaseId),
  });
  const create = useMutation({
    mutationFn: (kbId: string) => createConversation({ knowledge_base_id: kbId }),
  });

  // 深链接只带 conversation 时，用会话归属知识库补全 URL，避免侧栏/正文脱节。
  useEffect(() => {
    if (conversationId && !knowledgeBaseId && selectedConversation.data) {
      router.replace(
        `/conversations?knowledgeBase=${selectedConversation.data.knowledge_base_id}&conversation=${conversationId}`
      );
    }
  }, [conversationId, knowledgeBaseId, selectedConversation.data, router]);

  const applyKnowledgeBase = (value: string) => {
    closeCitationDrawer();
    router.replace(`/conversations?knowledgeBase=${value}`);
  };
  const selectKnowledgeBase = (value: string) => {
    if (value === knowledgeBaseId) return;
    applyKnowledgeBase(value);
  };

  // 空态输入即新建：先创建会话，再自动发送首条内容并更新 URL。
  const startConversation = async (content: string) => {
    if (!knowledgeBaseId) return;
    setStartError(null);
    try {
      const created = await create.mutateAsync(knowledgeBaseId);
      // 左侧会话历史共享同一 QueryClient；创建成功后立即失效，避免等待 30 秒 staleTime 或刷新页面。
      await queryClient.invalidateQueries({ queryKey: queryKeys.conversationsAll() });
      setDraft({ conversationId: created.id, content });
      router.replace(`/conversations?knowledgeBase=${knowledgeBaseId}&conversation=${created.id}`);
    } catch (err) {
      setStartError(asApiError(err));
    }
  };
  const onStarterKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter") return;
    if (event.nativeEvent.isComposing) return;
    if (event.ctrlKey || event.metaKey) {
      event.preventDefault();
      void startConversation(starterContent);
      return;
    }
    event.preventDefault();
    setStarterContent((current) => `${current}\n`.slice(0, 12_000));
  };

  const returnToList = () => {
    closeCitationDrawer();
    router.replace(
      knowledgeBaseId ? `/conversations?knowledgeBase=${knowledgeBaseId}` : "/conversations"
    );
  };

  return (
    <RequireAuth>
      <AppShell>
        {/* 主内容区（T151/T157）：会话历史位于全局侧栏，此处只承载对话正文。 */}
        <div className="flex min-h-[60dvh] flex-col lg:h-[calc(100dvh-7rem)]">
          <header className="shrink-0 border-b pb-5">
            <div className="flex flex-wrap items-end gap-4">
              <div>
                <h1 className="font-display text-2xl font-semibold">对话</h1>
                <p className="mt-1 text-sm text-muted-foreground">
                  所有回答均仅以当前知识库范围内已完成资料为证据。
                </p>
              </div>
              {conversationId ? (
                <div className="ml-auto flex items-center gap-2 rounded-md border border-clue/25 bg-clue/5 px-3 py-2 text-sm">
                  <Database className="h-4 w-4 text-clue" aria-hidden />
                  <span className="text-muted-foreground">基于知识库</span>
                  <span className="font-medium text-foreground">
                    {boundKnowledgeBase.data?.name ?? "正在加载…"}
                  </span>
                </div>
              ) : (
                <label className="grid gap-1 text-sm" htmlFor="conversation-knowledge-base">
                  当前知识库
                  <KnowledgeBasePicker value={knowledgeBaseId} onChange={selectKnowledgeBase} />
                </label>
              )}
            </div>
          </header>
          {conversationId && selectedConversation.isLoading ? (
            <div className="mt-4 flex min-h-0 flex-1 flex-col">
              <Skeleton className="h-40 w-full" aria-label="加载中" />
            </div>
          ) : null}
          {conversationId && selectedConversation.isError ? (
            <div className="mt-4 space-y-2">
              <ErrorState error={conversationErrorValue(selectedConversation.error)} />
              <button type="button" className="text-sm underline" onClick={returnToList}>
                返回对话列表
              </button>
            </div>
          ) : null}
          {conversationId && selectedConversation.data ? (
            <div className="mt-4 flex min-h-0 flex-1 flex-col">
              <MessageThread
                key={conversationId}
                conversationId={conversationId}
                initialDraft={draft?.conversationId === conversationId ? draft.content : undefined}
              />
              <CitationDrawer
                conversationId={conversationId}
                knowledgeBaseId={selectedConversation.data.knowledge_base_id}
              />
            </div>
          ) : null}
          {!conversationId ? (
            <div className="mt-4 flex flex-1 flex-col justify-center">
              {startError ? (
                <ErrorState
                  error={startError}
                  onRetry={() => void startConversation(starterContent)}
                />
              ) : null}
              {!knowledgeBaseId ? (
                <EmptyState
                  title="请选择知识库"
                  description="选择知识库后，即可输入内容开始新对话。"
                />
              ) : (
                <div className="mx-auto w-full max-w-2xl space-y-3">
                  <p className="text-center text-sm text-muted-foreground">
                    输入内容开始新对话，发送后自动创建会话历史
                  </p>
                  <div className="rounded-xl border bg-surface p-2 shadow-sm">
                    <label className="sr-only" htmlFor="starter-content">
                      输入问题
                    </label>
                    <textarea
                      id="starter-content"
                      className="min-h-24 w-full resize-none bg-transparent px-2 py-1 pr-10 text-sm outline-none focus:outline-none focus:ring-0"
                      value={starterContent}
                      onChange={(event) => setStarterContent(event.target.value.slice(0, 12_000))}
                      onKeyDown={onStarterKeyDown}
                      placeholder="输入问题，或在已有资料中检索…"
                    />
                    <div className="flex items-center justify-between px-1 pt-1">
                      <span className="text-xs text-muted-foreground">
                        Enter 换行 · Ctrl/Cmd+Enter 发送 · 上限 12,000 字
                      </span>
                      <Button
                        className="h-8 w-8 rounded-full p-0"
                        aria-label="发送"
                        disabled={!starterContent.trim() || create.isPending}
                        onClick={() => void startConversation(starterContent)}
                      >
                        <Send className="h-4 w-4" aria-hidden />
                      </Button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          ) : null}
        </div>
      </AppShell>
    </RequireAuth>
  );
}
