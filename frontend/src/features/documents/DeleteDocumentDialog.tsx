"use client";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import type { Document, ResourceAction } from "@/lib/api/types";
import { isTombstone } from "@/features/documents/status";

/**
 * 资料删除确认：操作类型只来自 Document.allowed_actions。
 * 20015 墓碑不暴露文件名、详情或处理入口，仅保留可诊断信息和重试删除。
 */
export function DeleteDocumentDialog({
  document,
  onDelete,
  pending = false,
}: {
  document: Document;
  onDelete: (action: ResourceAction) => void | Promise<void>;
  pending?: boolean;
}) {
  const action = document.allowed_actions.includes("retry_delete")
    ? "retry_delete"
    : document.allowed_actions.includes("delete")
      ? "delete"
      : null;
  const isRetry = action === "retry_delete";
  const deleteCleanupFailed = isTombstone(document);

  if (!action) return null;

  return (
    <AlertDialog>
      {deleteCleanupFailed ? (
        <div className="flex flex-col items-end gap-1">
          <span className="text-sm font-medium text-destructive">删除未完成</span>
          {document.error_message ? (
            <span className="text-sm text-destructive">
              <span>错误码 {document.error_code}</span>：<span>{document.error_message}</span>
            </span>
          ) : null}
        </div>
      ) : null}
      <AlertDialogTrigger asChild>
        <Button variant="destructive" disabled={pending}>
          {isRetry ? "重试删除" : "删除"}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogTitle>{isRetry ? "确认重试删除" : "确认删除"}</AlertDialogTitle>
        <AlertDialogDescription>
          {isRetry
            ? "将重新执行删除清理。"
            : "删除后资料将立即不可见，后台会继续清理文件和派生数据。"}
        </AlertDialogDescription>
        <div className="mt-5 flex justify-end gap-2">
          <AlertDialogCancel asChild>
            <Button variant="outline" disabled={pending}>
              取消
            </Button>
          </AlertDialogCancel>
          <AlertDialogAction asChild>
            <Button variant="destructive" disabled={pending} onClick={() => void onDelete(action)}>
              {isRetry ? "确认重试删除" : "确认删除"}
            </Button>
          </AlertDialogAction>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  );
}
