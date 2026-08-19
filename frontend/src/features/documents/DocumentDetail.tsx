"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { DeleteDocumentDialog } from "@/features/documents/DeleteDocumentDialog";
import { DocumentStatus } from "@/features/documents/DocumentStatus";
import { useDeleteDocument, useDocumentDetail } from "@/features/documents/queries";
import { isTombstone } from "@/features/documents/status";
import { TaskHistory } from "@/features/documents/TaskHistory";
import type { ResourceAction } from "@/lib/api/types";

/**
 * 资料详情（T112/T138/FR-005/010/011）：完整 DTO 渲染与 DTO 驱动轮询。
 * 异步失败以 HTTP 200 + error_code/error_message 表达；allowed_actions 来自服务端；
 * 删除成功后立即关闭详情；资料已不可见时不得用 404 覆盖旧的详情缓存。
 */
export function DocumentDetail({
  knowledgeBaseId,
  documentId,
  pollIntervalMs = 3000,
  onClose,
}: {
  knowledgeBaseId: string;
  documentId: string;
  pollIntervalMs?: number;
  onClose?: () => void;
}) {
  const detail = useDocumentDetail(knowledgeBaseId, documentId, { pollIntervalMs });
  const deleteMutation = useDeleteDocument();
  const [deleted, setDeleted] = useState(false);
  const doc = detail.data;

  const onDelete = async (action: ResourceAction) => {
    if (!doc) return;
    if (!doc.allowed_actions.includes(action)) return;
    try {
      await deleteMutation.mutateAsync({ knowledgeBaseId, documentId: doc.id });
      setDeleted(true);
      onClose?.();
    } catch {
      // 错误已由 mutation.error 呈现
    }
  };

  if (deleted) return null;

  if (detail.error && !doc) return <ErrorState error={detail.error} />;
  if (!doc) {
    if (detail.isLoading) return <Skeleton className="h-40 w-full" aria-label="加载中" />;
    return null;
  }

  const error = detail.error ?? deleteMutation.error;

  if (isTombstone(doc)) {
    return (
      <section aria-label="资料详情" className="space-y-2 rounded-md border px-3 py-2">
        {error ? <ErrorState error={error} /> : null}
        <div className="flex items-center justify-end gap-2">
          <DeleteDocumentDialog
            document={doc}
            pending={deleteMutation.isPending}
            onDelete={(action) => onDelete(action)}
          />
          {onClose ? (
            <Button variant="ghost" onClick={onClose}>
              关闭
            </Button>
          ) : null}
        </div>
      </section>
    );
  }

  return (
    <section aria-label="资料详情" className="space-y-2 rounded-md border px-3 py-2">
      {error ? <ErrorState error={error} /> : null}
      <div className="flex items-center justify-between gap-2">
        <h3 className="truncate font-medium">{doc.filename}</h3>
        <div className="flex shrink-0 gap-2">
          <DeleteDocumentDialog
            document={doc}
            pending={deleteMutation.isPending}
            onDelete={(action) => onDelete(action)}
          />
          {onClose ? (
            <Button variant="ghost" onClick={onClose}>
              关闭
            </Button>
          ) : null}
        </div>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <div>
          <dt className="text-muted-foreground">状态</dt>
          <dd>
            <DocumentStatus document={doc} />
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">当前阶段</dt>
          <dd>{doc.current_task_type ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">版本</dt>
          <dd>{doc.version}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">重试次数</dt>
          <dd>{doc.retry_count}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">片段数</dt>
          <dd>{doc.chunk_count}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">文件大小</dt>
          <dd>{doc.file_size} 字节</dd>
        </div>
      </dl>
      {doc.error_code != null && doc.error_message ? (
        <p className="text-sm text-destructive">
          错误码 {doc.error_code}：{doc.error_message}
        </p>
      ) : null}
      <TaskHistory knowledgeBaseId={knowledgeBaseId} documentId={doc.id} />
    </section>
  );
}
