"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  createConversation,
  deleteConversation,
  listConversations,
  listKnowledgeBases,
  renameConversation,
} from "@/lib/api/client";
import { queryKeys } from "@/lib/query-keys";
import type { Conversation } from "@/lib/api/types";

const PAGE_SIZE = 20;

/**
 * 会话列表只使用 API 已返回的知识库归属筛选展示；创建请求必须显式携带选中的 knowledge_base_id。
 * 授权与知识库有效性仍由服务端处理，客户端不推导授权结论。
 */
export function ConversationList({
  onSelectConversation,
  selectedConversationId,
}: {
  onSelectConversation?: (conversation: Conversation | null) => void;
  selectedConversationId?: string | null;
}) {
  const queryClient = useQueryClient();
  const [knowledgeBaseId, setKnowledgeBaseId] = useState("");
  const [page, setPage] = useState(1);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [titleDraft, setTitleDraft] = useState("");
  const knowledgeBases = useQuery({
    queryKey: queryKeys.knowledgeBases(1, 100),
    queryFn: () => listKnowledgeBases(1, 100),
  });
  const conversations = useQuery({
    queryKey: queryKeys.conversations(knowledgeBaseId, page, PAGE_SIZE),
    queryFn: () => listConversations(knowledgeBaseId, page, PAGE_SIZE),
    enabled: Boolean(knowledgeBaseId),
  });
  const create = useMutation({
    mutationFn: () => createConversation({ knowledge_base_id: knowledgeBaseId }),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversationsAll() });
      onSelectConversation?.(created);
    },
  });
  const rename = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => renameConversation(id, { title }),
    onSuccess: () => {
      setEditingId(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversationsAll() });
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: (_data, id) => {
      if (id === selectedConversationId) onSelectConversation?.(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.conversationsAll() });
    },
  });

  const activeKnowledgeBases =
    knowledgeBases.data?.items.filter((item) => item.status === "active") ?? [];
  const items = conversations.data?.items ?? [];
  const total = conversations.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const selectKnowledgeBase = (value: string) => {
    setKnowledgeBaseId(value);
    setPage(1);
  };

  return (
    <section className="space-y-4" aria-label="对话列表">
      <div className="flex flex-wrap items-end gap-3">
        <label className="grid gap-1 text-sm" htmlFor="conversation-knowledge-base">
          选择知识库
          <select
            id="conversation-knowledge-base"
            className="h-9 min-w-52 rounded-md border border-input bg-background px-3 text-sm"
            value={knowledgeBaseId}
            onChange={(event) => selectKnowledgeBase(event.currentTarget.value)}
          >
            <option value="">请选择知识库</option>
            {activeKnowledgeBases.map((knowledgeBase) => (
              <option key={knowledgeBase.id} value={knowledgeBase.id}>
                {knowledgeBase.name ?? "未命名知识库"}
              </option>
            ))}
          </select>
        </label>
        <Button disabled={!knowledgeBaseId || create.isPending} onClick={() => create.mutate()}>
          新建对话
        </Button>
      </div>

      {!knowledgeBaseId ? (
        <EmptyState title="请选择知识库" description="每个对话必须绑定一个可访问的知识库。" />
      ) : conversations.isLoading ? (
        <Skeleton className="h-32 w-full" aria-label="正在加载对话" />
      ) : conversations.isError ? (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm"
        >
          无法加载对话。请检查网络后重试。
          <Button className="ml-2" variant="outline" onClick={() => void conversations.refetch()}>
            重试
          </Button>
        </div>
      ) : items.length === 0 ? (
        <EmptyState title="尚无对话" description="创建对话后，即可围绕此知识库连续提问。" />
      ) : (
        <ul className="divide-y rounded-md border bg-surface">
          {items.map((conversation) => (
            <li
              key={conversation.id}
              className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-sm"
            >
              {editingId === conversation.id ? (
                <label className="flex items-center gap-2">
                  <span className="sr-only">新标题</span>
                  <input
                    aria-label="新标题"
                    className="h-8 rounded-md border border-input bg-background px-2"
                    value={titleDraft}
                    onChange={(event) => setTitleDraft(event.target.value)}
                  />
                  <Button
                    className="h-8"
                    disabled={!titleDraft.trim() || rename.isPending}
                    onClick={() => rename.mutate({ id: conversation.id, title: titleDraft.trim() })}
                  >
                    保存标题
                  </Button>
                </label>
              ) : (
                <Button
                  variant="ghost"
                  className="h-auto justify-start px-0 py-0 text-left"
                  aria-label={`打开对话 ${conversation.title || "未命名对话"}`}
                  onClick={() => onSelectConversation?.(conversation)}
                >
                  {conversation.title || "未命名对话"}
                </Button>
              )}
              <div className="flex gap-1">
                <Button
                  variant="ghost"
                  className="h-8 px-2"
                  aria-label={`重命名 ${conversation.title || "未命名对话"}`}
                  onClick={() => {
                    setEditingId(conversation.id);
                    setTitleDraft(conversation.title ?? "");
                  }}
                >
                  重命名
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 px-2 text-destructive"
                  aria-label={`删除对话 ${conversation.title || "未命名对话"}`}
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(conversation.id)}
                >
                  删除
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {knowledgeBaseId && pageCount > 1 ? (
        <nav className="flex items-center gap-2" aria-label="对话分页">
          <Button
            variant="outline"
            disabled={page === 1}
            onClick={() => setPage((value) => value - 1)}
          >
            上一页
          </Button>
          <span className="text-sm text-muted-foreground">第 {page} 页</span>
          <Button
            variant="outline"
            disabled={page >= pageCount}
            onClick={() => setPage((value) => value + 1)}
          >
            下一页
          </Button>
        </nav>
      ) : null}
    </section>
  );
}
