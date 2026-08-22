"use client";

import { useState } from "react";

import { ErrorState } from "@/components/ui/error-state";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { DeleteDocumentDialog } from "@/features/documents/DeleteDocumentDialog";
import { DocumentStatus } from "@/features/documents/DocumentStatus";
import { useDeleteDocument, useDocumentDetail } from "@/features/documents/queries";
import { isTombstone } from "@/features/documents/status";
import { TaskHistory, taskTypeLabel } from "@/features/documents/TaskHistory";
import { formatDateTime, formatFileSize } from "@/lib/format";
import type { ResourceAction } from "@/lib/api/types";

/**
 * 资料详情（T112/T138/FR-005/010/011）：完整 DTO 渲染与 DTO 驱动轮询。
 * 异步失败以 HTTP 200 + error_code/error_message 表达；allowed_actions 来自服务端；
 * 删除成功后立即关闭详情；资料已不可见时不得用 404 覆盖旧的详情缓存。
 * 20015 墓碑同样以可关闭抽屉呈现，保证所有详情分支都有退出路径。
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

  if (detail.error && !doc)
    return <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />;
  if (!doc) {
    if (detail.isLoading) return <Skeleton className="h-40 w-full" aria-label="加载中" />;
    return null;
  }

  const error = detail.error ?? deleteMutation.error;
  const stageLabel = doc.current_task_type ? taskTypeLabel[doc.current_task_type] : "—";

  return (
    <Sheet
      open
      onOpenChange={(open) => {
        if (!open) onClose?.();
      }}
    >
      <SheetContent aria-label="资料详情" side="right" className="space-y-2 overflow-y-auto">
        <SheetTitle>资料详情</SheetTitle>
        {error ? <ErrorState error={error} onRetry={() => void detail.refetch()} /> : null}
        <div className="flex items-center justify-between gap-2">
          <h3 className="truncate font-medium">{doc.filename}</h3>
          <div className="flex shrink-0 gap-2">
            <DeleteDocumentDialog
              document={doc}
              pending={deleteMutation.isPending}
              onDelete={(action) => onDelete(action)}
            />
          </div>
        </div>
        {isTombstone(doc) ? (
          <p className="text-sm text-destructive">删除未完成：该资料清理失败，仅可重试删除。</p>
        ) : null}
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          <div>
            <dt className="text-muted-foreground">状态</dt>
            <dd>
              <DocumentStatus document={doc} />
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">当前阶段</dt>
            <dd>{stageLabel}</dd>
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
            <dd>{formatFileSize(doc.file_size)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">创建时间</dt>
            <dd>{formatDateTime(doc.created_at)}</dd>
          </div>
          <div>
            <dt className="text-muted-foreground">完成时间</dt>
            <dd>{formatDateTime(doc.processing_finished_at)}</dd>
          </div>
        </dl>
        {doc.error_code != null && doc.error_message ? (
          <p className="text-sm text-destructive">
            错误码 {doc.error_code}：{doc.error_message}
          </p>
        ) : null}
        <TaskHistory knowledgeBaseId={knowledgeBaseId} documentId={doc.id} />
      </SheetContent>
    </Sheet>
  );
}
