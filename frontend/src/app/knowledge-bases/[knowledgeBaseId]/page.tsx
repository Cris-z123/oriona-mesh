"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";

import { AppShell } from "@/components/app-shell/AppShell";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { DocumentList } from "@/features/documents/DocumentList";
import { UploadPanel } from "@/features/documents/UploadPanel";
import { queryKeys } from "@/lib/query-keys";

export default function KnowledgeBaseDocumentsPage() {
  const params = useParams<{ knowledgeBaseId: string }>();
  const knowledgeBaseId = params.knowledgeBaseId;
  const queryClient = useQueryClient();

  /** 上传成功（202 已接受）后精确失效该知识库的资料子树，列表重取当前页。 */
  const onUploaded = () => {
    void queryClient.invalidateQueries({
      queryKey: queryKeys.documents(knowledgeBaseId),
      refetchType: "active",
    });
  };

  return (
    <RequireAuth>
      <AppShell contextRail={<p className="text-sm text-muted-foreground">当前知识库资料</p>}>
        <div className="space-y-6">
          <header>
            <h1 className="font-display text-2xl font-semibold">资料</h1>
            <p className="text-sm text-muted-foreground">上传并跟踪资料处理状态</p>
          </header>
          <UploadPanel knowledgeBaseId={knowledgeBaseId} onUploaded={onUploaded} />
          <DocumentList knowledgeBaseId={knowledgeBaseId} />
        </div>
      </AppShell>
    </RequireAuth>
  );
}
