"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";

import { AppShell } from "@/components/app-shell/AppShell";
import { CitationDrawer } from "@/features/citations/CitationDrawer";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { getConversation } from "@/lib/api/client";
import { queryKeys } from "@/lib/query-keys";

import { ConversationList } from "./ConversationList";
import { MessageThread } from "./MessageThread";

/** 会话页面的客户端交互边界：搜索参数、查询与路由选择仅在此处维护。 */
export function ConversationsWorkspace() {
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
              <MessageThread key={conversationId} conversationId={conversationId} />
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
