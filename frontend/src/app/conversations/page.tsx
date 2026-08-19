"use client";

import { AppShell } from "@/components/app-shell/AppShell";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { ConversationList } from "@/features/conversations/ConversationList";
import { MessageThread } from "@/features/conversations/MessageThread";
import { CitationDrawer } from "@/features/citations/CitationDrawer";
import { getConversation } from "@/lib/api/client";
import { queryKeys } from "@/lib/query-keys";
import { useQuery } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

/** 阶段 9 会话入口：具体会话与消息由列表选择后的页面状态接入。 */
export default function ConversationsPage() {
  return (
    <Suspense fallback={<div className="p-6 text-sm text-muted-foreground">正在加载对话…</div>}>
      <ConversationsWorkspace />
    </Suspense>
  );
}

function ConversationsWorkspace() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const conversationId = searchParams.get("conversation");
  const selectedConversation = useQuery({
    queryKey: queryKeys.conversationDetail(conversationId ?? ""),
    queryFn: () => getConversation(conversationId ?? ""),
    enabled: Boolean(conversationId),
  });

  const selectConversation = (conversation: { id: string } | null) => {
    router.replace(
      conversation ? `/conversations?conversation=${conversation.id}` : "/conversations"
    );
  };
  return (
    <RequireAuth>
      <AppShell>
        <div className="space-y-6">
          <header>
            <h1 className="font-display text-2xl font-semibold">对话</h1>
            <p className="text-sm text-muted-foreground">所有问题均在已绑定的知识库范围内回答。</p>
          </header>
          <ConversationList
            onSelectConversation={selectConversation}
            selectedConversationId={conversationId}
          />
          {conversationId && selectedConversation.data ? (
            <>
              <MessageThread conversationId={conversationId} />
              <CitationDrawer
                conversationId={conversationId}
                knowledgeBaseId={selectedConversation.data.knowledge_base_id}
              />
            </>
          ) : null}
        </div>
      </AppShell>
    </RequireAuth>
  );
}
