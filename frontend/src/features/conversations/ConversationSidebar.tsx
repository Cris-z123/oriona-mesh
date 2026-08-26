"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal } from "lucide-react";
import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState, toErrorStateValue } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { deleteConversation, listConversations, renameConversation } from "@/lib/api/client";
import { queryKeys } from "@/lib/query-keys";
import type { Conversation } from "@/lib/api/types";
import { useUiStore } from "@/stores/ui-store";

const PAGE_SIZE = 20;

/**
 * 对话工作区会话历史侧栏（T157/T173，ui-design §3.1）：始终承载当前用户的
 * 全局会话历史（分页，跨知识库），每行展示所属知识库名称；重命名/删除收进
 * 扩展菜单。知识库是回答与新对话的必选边界，但不是浏览本人历史的前置条件。
 */
export function ConversationSidebar({ onNavigate }: { onNavigate?: () => void }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const closeCitationDrawer = useUiStore((state) => state.closeCitationDrawer);
  const conversationId = searchParams.get("conversation");
  const urlKnowledgeBaseId = searchParams.get("knowledgeBase") ?? "";
  const [page, setPage] = useState(1);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [titleDraft, setTitleDraft] = useState("");
  const [retry, setRetry] = useState<(() => void) | null>(null);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);

  // 全局历史不依赖 URL 知识库参数；正文顶部的选择器只决定新建对话范围。
  const conversations = useQuery({
    queryKey: queryKeys.conversationsGlobal(page, PAGE_SIZE),
    queryFn: () => listConversations(undefined, page, PAGE_SIZE),
  });
  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: queryKeys.conversationsAll() });

  const openConversation = (conversation: Conversation) => {
    closeCitationDrawer();
    // 原子恢复会话及其绑定知识库的 URL 上下文，不触发正文顶部选择器的确认。
    router.push(
      `/conversations?knowledgeBase=${conversation.knowledge_base_id}&conversation=${conversation.id}`
    );
    onNavigate?.();
  };

  const rename = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => renameConversation(id, { title }),
    onSuccess: () => {
      invalidate();
      setEditingId(null);
      setRetry(null);
      notify("已保存");
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: (_value, id) => {
      invalidate();
      setRetry(null);
      closeCitationDrawer();
      notify("已删除");
      if (id === conversationId) {
        // 删除当前会话后回到全局历史；保留 URL 知识库参数可继续在该范围新建对话。
        router.replace(
          urlKnowledgeBaseId
            ? `/conversations?knowledgeBase=${urlKnowledgeBaseId}`
            : "/conversations"
        );
      }
    },
  });

  const error = rename.error ?? remove.error ?? conversations.error;
  const saveTitle = (id: string) => {
    const action = () => rename.mutate({ id, title: titleDraft.trim() });
    setRetry(() => action);
    action();
  };
  const deleteNow = (id: string) => {
    const action = () => remove.mutate(id);
    setRetry(() => action);
    return remove.mutateAsync(id).catch(() => undefined);
  };
  const items = conversations.data?.items ?? [];
  const lastPage = Math.max(1, Math.ceil((conversations.data?.total ?? 0) / PAGE_SIZE));

  return (
    <div className="space-y-3 p-2">
      {error ? (
        <ErrorState
          error={toErrorStateValue(error, "操作失败，请重试。")}
          onRetry={retry ?? (() => void conversations.refetch())}
        />
      ) : null}
      {conversations.isLoading ? (
        <Skeleton className="h-32 w-full" aria-label="正在加载对话" />
      ) : conversations.isError ? null : items.length === 0 ? (
        <EmptyState title="尚无对话" description="在主内容区输入内容，即可开始新对话。" />
      ) : (
        <ul className="divide-y rounded-md border bg-surface">
          {items.map((conversation) => (
            <li
              key={conversation.id}
              className="flex items-center justify-between gap-1 px-3 py-2 text-sm"
            >
              {editingId === conversation.id ? (
                <label className="flex min-w-0 flex-1 items-center gap-2">
                  <span className="sr-only">新标题</span>
                  <input
                    aria-label="新标题"
                    className="h-8 w-28 rounded-md border border-input bg-background px-2"
                    value={titleDraft}
                    onChange={(event) => setTitleDraft(event.target.value)}
                  />
                  <Button
                    className="h-8"
                    disabled={!titleDraft.trim() || rename.isPending}
                    onClick={() => saveTitle(conversation.id)}
                  >
                    保存标题
                  </Button>
                </label>
              ) : (
                <>
                  <Button
                    variant="ghost"
                    className="h-auto min-w-0 flex-1 justify-start px-1 py-1 text-left"
                    aria-label={`打开对话 ${conversation.title || "未命名对话"}`}
                    onClick={() => openConversation(conversation)}
                  >
                    <span className="flex min-w-0 flex-col items-start">
                      <span className="truncate">{conversation.title || "未命名对话"}</span>
                      {/* 所属知识库名称（T172 授权投影）作为次级标签。 */}
                      <span className="truncate text-xs text-muted-foreground">
                        {conversation.knowledge_base_name}
                      </span>
                    </span>
                  </Button>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        variant="ghost"
                        className="h-8 w-8 shrink-0 p-0"
                        aria-label={`会话操作 ${conversation.title || "未命名对话"}`}
                      >
                        <MoreHorizontal className="h-4 w-4" aria-hidden />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent>
                      <DropdownMenuItem
                        onSelect={() => {
                          setEditingId(conversation.id);
                          setTitleDraft(conversation.title ?? "");
                        }}
                      >
                        重命名
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => setPendingDeleteId(conversation.id)}>
                        删除
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {conversations.data && conversations.data.total > PAGE_SIZE ? (
        <div className="flex items-center justify-between text-xs">
          <Button
            variant="ghost"
            className="h-7 px-2"
            disabled={page <= 1 || conversations.isFetching}
            onClick={() => setPage((value) => value - 1)}
          >
            上一页
          </Button>
          <span className="text-muted-foreground">
            {page} / {lastPage}
          </span>
          <Button
            variant="ghost"
            className="h-7 px-2"
            disabled={page >= lastPage || conversations.isFetching}
            onClick={() => setPage((value) => value + 1)}
          >
            下一页
          </Button>
        </div>
      ) : null}

      {/* 删除确认（从扩展菜单进入）：受控弹窗，避免 destructive 按钮混入菜单。 */}
      <AlertDialog
        open={pendingDeleteId !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDeleteId(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogTitle>确认删除对话</AlertDialogTitle>
          <AlertDialogDescription>删除后该对话及其消息将不可恢复。</AlertDialogDescription>
          <div className="flex justify-end gap-2">
            <AlertDialogCancel asChild>
              <Button variant="outline">取消</Button>
            </AlertDialogCancel>
            <AlertDialogAction asChild>
              <Button
                variant="destructive"
                onClick={() => {
                  if (pendingDeleteId) void deleteNow(pendingDeleteId);
                  setPendingDeleteId(null);
                }}
              >
                确认删除
              </Button>
            </AlertDialogAction>
          </div>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
