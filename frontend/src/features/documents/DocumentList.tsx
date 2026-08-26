"use client";

import { useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal, Upload } from "lucide-react";
import { useState } from "react";

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
import { ErrorState } from "@/components/ui/error-state";
import { Pagination, pageAfterDeletingLastItem } from "@/components/ui/pagination";
import { DeleteDocumentDialog } from "@/features/documents/DeleteDocumentDialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { queryKeys } from "@/lib/query-keys";
import type { Document, DocumentStatusFilter, ResourceAction } from "@/lib/api/types";
import { DocumentDetail } from "@/features/documents/DocumentDetail";
import { DocumentStatus } from "@/features/documents/DocumentStatus";
import { useDeleteDocument, useDocumentList } from "@/features/documents/queries";
import { isTombstone } from "@/features/documents/status";
import { taskTypeLabel } from "@/features/documents/TaskHistory";
import { formatDateTime } from "@/lib/format";
import { useUiStore } from "@/stores/ui-store";

const PAGE_SIZE = 20;

/**
 * 资料列表（T112/T138/FR-005/010/011）：页码/状态列表、失败原因与 allowed_actions 渲染。
 * 数据由统一 Query 封装管理：存在处理中资料时按 pollIntervalMs 轮询直到全部终态；
 * 状态过滤是非敏感视图偏好，保存在 UI store；
 * 20015 墓碑只显示最小“删除未完成”与重试删除，不作为普通失败资料展示。
 */
export function DocumentList({
  knowledgeBaseId,
  initialDocumentId = null,
  pollIntervalMs = 3000,
  onUpload,
}: {
  knowledgeBaseId: string;
  initialDocumentId?: string | null;
  pollIntervalMs?: number;
  onUpload?: () => void;
}) {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(initialDocumentId);
  const [pendingDelete, setPendingDelete] = useState<{
    document: Document;
    action: ResourceAction;
  } | null>(null);
  const statusFilter = useUiStore((state) => state.documentStatusFilter);
  const setStatusFilter = useUiStore((state) => state.setDocumentStatusFilter);

  const list = useDocumentList(knowledgeBaseId, page, PAGE_SIZE, statusFilter, {
    pollIntervalMs,
  });
  const deleteMutation = useDeleteDocument();

  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const error = list.error ?? deleteMutation.error;

  const onDelete = async (doc: Document, action: ResourceAction) => {
    if (!doc.allowed_actions.includes(action)) return;
    try {
      await deleteMutation.mutateAsync({ knowledgeBaseId, documentId: doc.id });
      const nextPage = pageAfterDeletingLastItem(page, items.length);
      if (nextPage !== page) {
        setPage(nextPage);
      } else {
        await queryClient.refetchQueries({ queryKey: queryKeys.documents(knowledgeBaseId) });
      }
    } catch {
      // 错误已由 mutation.error 呈现
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-4">
      {error ? <ErrorState error={error} onRetry={() => void list.refetch()} /> : null}

      <div className="flex items-center gap-2">
        <Select
          value={statusFilter}
          onValueChange={(v) => setStatusFilter(v as DocumentStatusFilter)}
        >
          <SelectTrigger aria-label="状态过滤" className="w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部状态</SelectItem>
            <SelectItem value="pending">待处理</SelectItem>
            <SelectItem value="queued">排队中</SelectItem>
            <SelectItem value="processing">处理中</SelectItem>
            <SelectItem value="completed">已完成</SelectItem>
            <SelectItem value="failed">失败</SelectItem>
          </SelectContent>
        </Select>
        <span className="text-sm text-muted-foreground">共 {total} 份资料</span>
      </div>

      {list.isLoading ? (
        <ul className="space-y-2" aria-label="加载中">
          {[0, 1, 2].map((i) => (
            <li key={i}>
              <Skeleton className="h-14 w-full" />
            </li>
          ))}
        </ul>
      ) : items.length === 0 && !error ? (
        <EmptyState
          title="暂无资料"
          description="上传第一份资料后在此查看处理状态"
          action={
            onUpload ? (
              <Button variant="outline" onClick={onUpload}>
                <Upload className="h-4 w-4" aria-hidden />
                上传资料
              </Button>
            ) : undefined
          }
        />
      ) : (
        <ul className="space-y-2" aria-busy={list.isFetching || undefined}>
          {items.map((doc) => (
            <li key={doc.id} className="rounded-md border px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  {!isTombstone(doc) ? (
                    <>
                      <p className="truncate font-medium">{doc.filename}</p>
                      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted-foreground">
                        <DocumentStatus document={doc} />
                        <span>
                          阶段：{doc.current_task_type ? taskTypeLabel[doc.current_task_type] : "—"}
                        </span>
                        <span>更新：{formatDateTime(doc.updated_at)}</span>
                      </div>
                    </>
                  ) : null}
                  {!isTombstone(doc) && doc.status === "failed" && doc.error_message ? (
                    <p className="text-sm text-destructive">{doc.error_message}</p>
                  ) : null}
                </div>
                <div className="flex shrink-0 gap-2">
                  {isTombstone(doc) ? (
                    <DeleteDocumentDialog
                      document={doc}
                      pending={deleteMutation.isPending}
                      onDelete={(action) => onDelete(doc, action)}
                    />
                  ) : (
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          className="h-9 w-9 p-0"
                          aria-label={`资料操作 ${doc.filename}`}
                          title={`资料操作 ${doc.filename}`}
                        >
                          <MoreHorizontal className="h-4 w-4" aria-hidden />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent>
                        <DropdownMenuItem onSelect={() => setSelectedId(doc.id)}>
                          查看详情
                        </DropdownMenuItem>
                        {doc.allowed_actions.includes("delete") ? (
                          <DropdownMenuItem
                            onSelect={() => setPendingDelete({ document: doc, action: "delete" })}
                          >
                            删除
                          </DropdownMenuItem>
                        ) : null}
                      </DropdownMenuContent>
                    </DropdownMenu>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}

      {selectedId ? (
        <DocumentDetail
          knowledgeBaseId={knowledgeBaseId}
          documentId={selectedId}
          onClose={() => setSelectedId(null)}
        />
      ) : null}

      <AlertDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogTitle>确认删除资料</AlertDialogTitle>
          <AlertDialogDescription>
            删除后资料将立即不可见，后台会继续清理文件和派生数据。
          </AlertDialogDescription>
          <div className="mt-5 flex justify-end gap-2">
            <AlertDialogCancel asChild>
              <Button variant="outline" disabled={deleteMutation.isPending}>
                取消
              </Button>
            </AlertDialogCancel>
            <AlertDialogAction asChild>
              <Button
                variant="destructive"
                disabled={deleteMutation.isPending}
                onClick={() => {
                  if (pendingDelete) void onDelete(pendingDelete.document, pendingDelete.action);
                  setPendingDelete(null);
                }}
              >
                确认删除
              </Button>
            </AlertDialogAction>
          </div>
        </AlertDialogContent>
      </AlertDialog>

      <Pagination
        page={page}
        pageCount={totalPages}
        isFetching={list.isFetching}
        onPageChange={setPage}
      />
    </div>
  );
}
