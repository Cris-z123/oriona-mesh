"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { Button } from "@/components/ui/button";
import { ApiError, asApiError, deleteDocument, listDocuments } from "@/lib/api/client";
import type { Document, DocumentStatus } from "@/lib/api/types";
import { DocumentDetail } from "@/features/documents/DocumentDetail";
import { isInFlight, statusLabel } from "@/features/documents/status";

const PAGE_SIZE = 20;

/** failed/delete_cleanup/20015：服务端标记的“删除未完成”最小墓碑。 */
function isTombstone(doc: Document): boolean {
  return doc.error_code === 20015;
}

/**
 * 资料列表（T112/FR-005/010/011）：页码/状态列表、失败原因与 allowed_actions 渲染；
 * 存在处理中资料时按 pollIntervalMs 轮询直到全部终态；
 * 20015 墓碑只显示最小“删除未完成”与重试删除，不作为普通失败资料展示。
 */
export function DocumentList({
  knowledgeBaseId,
  pollIntervalMs = 3000,
}: {
  knowledgeBaseId: string;
  pollIntervalMs?: number;
}) {
  const [items, setItems] = useState<Document[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [statusFilter, setStatusFilter] = useState<DocumentStatus | "all">("all");
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await listDocuments(
        knowledgeBaseId,
        page,
        PAGE_SIZE,
        statusFilter === "all" ? undefined : statusFilter
      );
      setItems(result.items);
      setTotal(result.total);
      setError(null);
    } catch (err) {
      setError(asApiError(err));
    } finally {
      setLoading(false);
    }
  }, [knowledgeBaseId, page, statusFilter]);

  useEffect(() => {
    // 延迟到宏任务执行首次加载：避免在 effect 内同步触发 setState
    const timer = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    if (items.some((doc) => isInFlight(doc.status))) {
      const timer = setTimeout(() => {
        void load();
      }, pollIntervalMs);
      return () => clearTimeout(timer);
    }
  }, [items, load, pollIntervalMs]);

  const onDelete = async (doc: Document) => {
    setError(null);
    try {
      await deleteDocument(knowledgeBaseId, doc.id);
      await load();
    } catch (err) {
      setError(asApiError(err));
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-4">
      {error ? <ApiErrorNotice error={error} /> : null}

      <div className="flex items-center gap-2">
        <select
          aria-label="状态过滤"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as DocumentStatus | "all")}
          className="h-9 rounded-md border border-input bg-background px-2 text-sm"
        >
          <option value="all">全部状态</option>
          <option value="pending">待处理</option>
          <option value="queued">排队中</option>
          <option value="processing">处理中</option>
          <option value="completed">已完成</option>
          <option value="failed">失败</option>
        </select>
        <span className="text-sm text-muted-foreground">共 {total} 份资料</span>
      </div>

      <ul className="space-y-2">
        {items.map((doc) => (
          <li key={doc.id} className="rounded-md border px-3 py-2">
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate font-medium">{doc.filename}</p>
                <p className="text-sm text-muted-foreground">{statusLabel(doc.status)}</p>
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
          disabled={page <= 1 || loading}
          onClick={() => setPage((p) => p - 1)}
        >
          上一页
        </Button>
        <span>
          第 {page} / {totalPages} 页
        </span>
        <Button
          variant="outline"
          disabled={page >= totalPages || loading}
          onClick={() => setPage((p) => p + 1)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}
