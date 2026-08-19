"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { queryKeys } from "@/lib/query-keys";
import type { Document } from "@/lib/api/types";
import { DocumentDetail } from "@/features/documents/DocumentDetail";
import { useDeleteDocument, useDocumentList } from "@/features/documents/queries";
import { isTombstone, statusLabel } from "@/features/documents/status";
import { useUiStore, type DocumentStatusFilter } from "@/stores/ui-store";

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
}: {
  knowledgeBaseId: string;
  initialDocumentId?: string | null;
  pollIntervalMs?: number;
}) {
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(initialDocumentId);
  const statusFilter = useUiStore((state) => state.documentStatusFilter);
  const setStatusFilter = useUiStore((state) => state.setDocumentStatusFilter);

  const list = useDocumentList(knowledgeBaseId, page, PAGE_SIZE, statusFilter, {
    pollIntervalMs,
  });
  const deleteMutation = useDeleteDocument();

  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const error = list.error ?? deleteMutation.error;

  const onDelete = async (doc: Document) => {
    try {
      await deleteMutation.mutateAsync({ knowledgeBaseId, documentId: doc.id });
      // 末页最后一项删除后回退一页（键变更后按挂载重取过期数据）
      if (items.length === 1 && page > 1) {
        setPage((p) => p - 1);
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
      {error ? <ErrorState error={error} /> : null}

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
      ) : items.length === 0 ? (
        <EmptyState title="暂无资料" description="上传第一份资料后在此查看处理状态" />
      ) : (
        <ul className="space-y-2" aria-busy={list.isFetching || undefined}>
          {items.map((doc) => (
            <li key={doc.id} className="rounded-md border px-3 py-2">
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate font-medium">{doc.filename}</p>
                  {!isTombstone(doc) ? (
                    <div className="mt-0.5 flex items-center gap-2">
                      <Badge variant="secondary">{statusLabel(doc.status)}</Badge>
                    </div>
                  ) : null}
                  {doc.status === "failed" && doc.error_message ? (
                    <p className="text-sm text-destructive">{doc.error_message}</p>
                  ) : null}
                </div>
                <div className="flex shrink-0 gap-2">
                  {isTombstone(doc) ? (
                    <>
                      <span className="text-sm font-medium text-destructive">删除未完成</span>
                      <Button variant="destructive" onClick={() => void onDelete(doc)}>
                        重试删除
                      </Button>
                    </>
                  ) : doc.allowed_actions.includes("delete") ? (
                    <Button variant="destructive" onClick={() => void onDelete(doc)}>
                      删除
                    </Button>
                  ) : null}
                  <Button variant="outline" onClick={() => setSelectedId(doc.id)}>
                    详情
                  </Button>
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

      <div className="flex items-center gap-2 text-sm">
        <Button
          variant="outline"
          disabled={page <= 1 || list.isFetching}
          onClick={() => setPage((p) => p - 1)}
        >
          上一页
        </Button>
        <span>
          第 {page} / {totalPages} 页
        </span>
        <Button
          variant="outline"
          disabled={page >= totalPages || list.isFetching}
          onClick={() => setPage((p) => p + 1)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}
