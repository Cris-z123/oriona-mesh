"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { AppShell } from "@/components/app-shell/AppShell";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { DocumentList } from "@/features/documents/DocumentList";
import { UploadPanel } from "@/features/documents/UploadPanel";
import { isInFlight } from "@/features/documents/status";
import { getKnowledgeBase, listDocuments } from "@/lib/api/client";
import { queryKeys } from "@/lib/query-keys";
import type { Document } from "@/lib/api/types";

export default function KnowledgeBaseDocumentsPage() {
  return (
    <Suspense fallback={<div className="p-6 text-sm text-muted-foreground">正在加载资料…</div>}>
      <KnowledgeBaseDocumentsWorkspace />
    </Suspense>
  );
}

function KnowledgeBaseDocumentsWorkspace() {
  const params = useParams<{ knowledgeBaseId: string }>();
  const searchParams = useSearchParams();
  const knowledgeBaseId = params.knowledgeBaseId;
  const documentId = searchParams.get("document");
  const queryClient = useQueryClient();
  const pendingIds = useRef<Set<string>>(new Set());
  const [trackingUploads, setTrackingUploads] = useState(false);

  // 标题展示所属知识库（T158）。
  const knowledgeBase = useQuery({
    queryKey: queryKeys.knowledgeBaseDetail(knowledgeBaseId),
    queryFn: () => getKnowledgeBase(knowledgeBaseId),
  });

  /** 上传接受后：立即重取当前页使非终态行可见（T158），并开始跨筛选批次跟踪。 */
  const onUploaded = (documents: Document[]) => {
    for (const document of documents) {
      if (isInFlight(document.status)) pendingIds.current.add(document.id);
    }
    setTrackingUploads(pendingIds.current.size > 0);
    void queryClient.invalidateQueries({ queryKey: queryKeys.documents(knowledgeBaseId) });
  };

  // 跨筛选跟踪批次（T150）：status=all 的查询与当前筛选解耦，
  // 批次全部终态后精确失效资料子树，让当前筛选列表与详情抽屉重取。
  const batchQuery = useQuery({
    queryKey: queryKeys.documentList(knowledgeBaseId, 1, 100, "all"),
    // 后端公开状态枚举不含 "all"：省略 status 参数即不过滤。
    queryFn: () => listDocuments(knowledgeBaseId, 1, 100, undefined),
    enabled: trackingUploads,
    refetchInterval: trackingUploads ? 3_000 : false,
  });

  useEffect(() => {
    if (!trackingUploads || !batchQuery.data) return;
    const byId = new Map(batchQuery.data.items.map((document) => [document.id, document]));
    const remaining = [...pendingIds.current].filter((id) => {
      const document = byId.get(id);
      // 不在该页（理论上新上传必在第一页）或仍在处理中 → 继续跟踪。
      return !document || isInFlight(document.status);
    });
    if (remaining.length === 0) {
      pendingIds.current.clear();
      setTrackingUploads(false);
      void queryClient.invalidateQueries({ queryKey: queryKeys.documents(knowledgeBaseId) });
    }
  }, [batchQuery.data, knowledgeBaseId, queryClient, trackingUploads]);

  return (
    <RequireAuth>
      <AppShell>
        <div className="space-y-6">
          <header>
            <h1 className="font-display text-2xl font-semibold">
              资料{knowledgeBase.data?.name ? ` — ${knowledgeBase.data.name}` : ""}
            </h1>
            <p className="text-sm text-muted-foreground">上传并跟踪资料处理状态</p>
          </header>
          <UploadPanel knowledgeBaseId={knowledgeBaseId} onUploaded={onUploaded} />
          <DocumentList knowledgeBaseId={knowledgeBaseId} initialDocumentId={documentId} />
        </div>
      </AppShell>
    </RequireAuth>
  );
}
