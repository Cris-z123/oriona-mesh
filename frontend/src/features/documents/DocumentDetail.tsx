"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { Button } from "@/components/ui/button";
import { ApiError, asApiError, deleteDocument, getDocument } from "@/lib/api/client";
import type { Document } from "@/lib/api/types";
import { isInFlight, isTombstone, statusLabel } from "@/features/documents/status";

/**
 * 资料详情（T112/FR-005/010/011）：完整 DTO 渲染与处理中轮询；
 * 异步失败以 HTTP 200 + error_code/error_message 表达；allowed_actions 来自服务端。
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
  const [doc, setDoc] = useState<Document | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await getDocument(knowledgeBaseId, documentId);
      setDoc(result);
      setError(null);
    } catch (err) {
      setError(asApiError(err));
    }
  }, [knowledgeBaseId, documentId]);

  useEffect(() => {
    // 延迟到宏任务：react-hooks/set-state-in-effect 要求 effect 内不得同步 setState
    const timer = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    if (doc && isInFlight(doc.status)) {
      const timer = setTimeout(() => {
        void load();
      }, pollIntervalMs);
      return () => clearTimeout(timer);
    }
  }, [doc, load, pollIntervalMs]);

  const onDelete = async () => {
    if (!doc) return;
    setError(null);
    try {
      await deleteDocument(knowledgeBaseId, doc.id);
      await load();
    } catch (err) {
      setError(asApiError(err));
    }
  };

  if (error && !doc) return <ApiErrorNotice error={error} />;
  if (!doc) return null;

  return (
    <section aria-label="资料详情" className="space-y-2 rounded-md border px-3 py-2">
      {error ? <ApiErrorNotice error={error} /> : null}
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
          <dd>{statusLabel(doc.status)}</dd>
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
