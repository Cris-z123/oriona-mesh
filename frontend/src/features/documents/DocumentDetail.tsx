"use client";

import { useQueryClient } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { queryKeys, useDeleteDocument, useDocumentDetail } from "@/features/documents/queries";
import { isTombstone, statusLabel } from "@/features/documents/status";

/**
 * 资料详情（T112/T138/FR-005/010/011）：完整 DTO 渲染与 DTO 驱动轮询。
 * 异步失败以 HTTP 200 + error_code/error_message 表达；allowed_actions 来自服务端；
 * 删除成功后重取详情，使删除收敛状态或 404 成为界面真相。
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
  const queryClient = useQueryClient();
  const detail = useDocumentDetail(knowledgeBaseId, documentId, { pollIntervalMs });
  const deleteMutation = useDeleteDocument();
  const doc = detail.data;

  const onDelete = async () => {
    if (!doc) return;
    try {
      await deleteMutation.mutateAsync({ knowledgeBaseId, documentId: doc.id });
      await queryClient.refetchQueries({
        queryKey: queryKeys.documentDetail(knowledgeBaseId, doc.id),
      });
    } catch {
      // 错误已由 mutation.error 呈现
    }
  };

  if (detail.error && !doc) return <ErrorState error={detail.error} />;
  if (!doc) {
    if (detail.isLoading) return <Skeleton className="h-40 w-full" aria-label="加载中" />;
    return null;
  }

  const error = detail.error ?? deleteMutation.error;

  return (
    <section aria-label="资料详情" className="space-y-2 rounded-md border px-3 py-2">
      {error ? <ErrorState error={error} /> : null}
      <div className="flex items-center justify-between gap-2">
        <h3 className="truncate font-medium">{doc.filename}</h3>
        <div className="flex shrink-0 gap-2">
          {isTombstone(doc) ? (
            <>
              <span className="text-sm font-medium text-destructive">删除未完成</span>
              <Button variant="destructive" onClick={() => void onDelete()}>
                重试删除
              </Button>
            </>
          ) : doc.allowed_actions.includes("delete") ? (
            <Button variant="destructive" onClick={() => void onDelete()}>
              删除
            </Button>
          ) : null}
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
            <Badge variant={isTombstone(doc) ? "destructive" : "secondary"}>
              {statusLabel(doc.status)}
            </Badge>
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
    </section>
  );
}
