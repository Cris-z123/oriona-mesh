"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { DangerousActionDialog } from "@/components/ui/dangerous-action-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Pagination, pageAfterDeletingLastItem } from "@/components/ui/pagination";
import { Skeleton } from "@/components/ui/skeleton";
import type { KnowledgeBase } from "@/lib/api/types";
import {
  useCreateKnowledgeBase,
  useDeleteKnowledgeBase,
  useKnowledgeBaseList,
  useUpdateKnowledgeBase,
} from "@/features/knowledge-bases/queries";
import { queryKeys } from "@/lib/query-keys";

const PAGE_SIZE = 20;

/**
 * 知识库列表（T110/T138/FR-003）：页码列表、创建、编辑与删除。
 * - 列表数据由 TanStack Query 管理；写成功后精确失效并按需重取当前页；
 * - delete_failed/20015 仅显示最小“删除未完成”墓碑与 retry_delete；
 * - 展示内容与操作全部来自服务端 DTO（name/description/allowed_actions），不自行推导。
 */
export function KnowledgeBaseList() {
  const queryClient = useQueryClient();
  const deletingIdsRef = useRef(new Set<string>());
  const [page, setPage] = useState(1);

  const [createName, setCreateName] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");

  const list = useKnowledgeBaseList(page, PAGE_SIZE);
  const create = useCreateKnowledgeBase();
  const update = useUpdateKnowledgeBase();
  const remove = useDeleteKnowledgeBase();

  const items = list.data?.items ?? [];
  const total = list.data?.total ?? 0;
  const mutationError = create.error ?? update.error ?? remove.error;
  const error = list.error ?? mutationError;

  /** 写成功后重取当前活动页（非末页回退场景）。 */
  const refreshList = async () => {
    await queryClient.refetchQueries({ queryKey: queryKeys.knowledgeBasesAll() });
  };

  const onCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    try {
      await create.mutateAsync({
        name: createName.trim(),
        ...(createDescription.trim() !== "" ? { description: createDescription.trim() } : {}),
      });
      setCreateName("");
      setCreateDescription("");
      setPage(1);
      await refreshList();
    } catch {
      // 错误已由 mutation.error 呈现
    }
  };

  const onDelete = async (kb: KnowledgeBase) => {
    // React 状态的 pending 在同一事件循环内尚未更新；用 ref 拦截双击等同步重入。
    if (deletingIdsRef.current.has(kb.id)) return;
    deletingIdsRef.current.add(kb.id);
    try {
      await remove.mutateAsync(kb.id);
      const nextPage = pageAfterDeletingLastItem(page, items.length);
      if (nextPage !== page) {
        setPage(nextPage);
      } else {
        await refreshList();
      }
    } catch {
      // 错误已由 mutation.error 呈现
    } finally {
      deletingIdsRef.current.delete(kb.id);
    }
  };

  const startEdit = (kb: KnowledgeBase) => {
    setEditingId(kb.id);
    setEditName(kb.name ?? "");
    setEditDescription(kb.description ?? "");
  };

  const onSaveEdit = async (kb: KnowledgeBase) => {
    try {
      await update.mutateAsync({
        id: kb.id,
        input: {
          name: editName.trim(),
          ...(editDescription.trim() !== "" ? { description: editDescription.trim() } : {}),
        },
      });
      setEditingId(null);
      await refreshList();
    } catch {
      // 错误已由 mutation.error 呈现
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-4">
      {error ? <ErrorState error={error} /> : null}

      <form onSubmit={onCreate} className="flex flex-wrap items-end gap-2">
        <div className="space-y-1.5">
          <Label htmlFor="kb-name">知识库名称</Label>
          <Input
            id="kb-name"
            value={createName}
            onChange={(e) => setCreateName(e.target.value)}
            maxLength={120}
            required
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="kb-description">描述</Label>
          <Input
            id="kb-description"
            value={createDescription}
            onChange={(e) => setCreateDescription(e.target.value)}
            maxLength={1000}
          />
        </div>
        <Button type="submit" disabled={create.isPending || createName.trim() === ""}>
          创建
        </Button>
      </form>

      {list.isLoading ? (
        <ul className="space-y-2" aria-label="加载中">
          {[0, 1, 2].map((i) => (
            <li key={i}>
              <Skeleton className="h-14 w-full" />
            </li>
          ))}
        </ul>
      ) : items.length === 0 ? (
        <EmptyState title="暂无知识库" description="创建第一个知识库开始整理资料" />
      ) : (
        <ul className="space-y-2" aria-busy={list.isFetching || undefined}>
          {items.map((kb) =>
            kb.status === "delete_failed" ? (
              <li
                key={kb.id}
                className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <div>
                    <p className="font-medium text-destructive">删除未完成</p>
                    <p className="text-sm text-muted-foreground">
                      知识库删除未完成，请重试删除（20015）
                    </p>
                  </div>
                  <DangerousActionDialog
                    triggerLabel="重试删除"
                    title="确认重试删除知识库"
                    description="将重新执行知识库及其资料的删除清理。"
                    confirmLabel="确认重试删除"
                    pending={remove.isPending}
                    onConfirm={() => onDelete(kb)}
                  />
                </div>
              </li>
            ) : editingId === kb.id ? (
              <li key={kb.id} className="rounded-md border px-3 py-2">
                <div className="space-y-2">
                  <div className="space-y-1.5">
                    <Label htmlFor={`edit-name-${kb.id}`}>名称</Label>
                    <Input
                      id={`edit-name-${kb.id}`}
                      value={editName}
                      onChange={(e) => setEditName(e.target.value)}
                      maxLength={120}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor={`edit-desc-${kb.id}`}>描述</Label>
                    <Input
                      id={`edit-desc-${kb.id}`}
                      value={editDescription}
                      onChange={(e) => setEditDescription(e.target.value)}
                      maxLength={1000}
                    />
                  </div>
                  <div className="flex gap-2">
                    <Button onClick={() => void onSaveEdit(kb)}>保存</Button>
                    <Button variant="ghost" onClick={() => setEditingId(null)}>
                      取消
                    </Button>
                  </div>
                </div>
              </li>
            ) : (
              <li key={kb.id} className="rounded-md border px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate font-medium">{kb.name}</p>
                    {kb.description ? (
                      <p className="truncate text-sm text-muted-foreground">{kb.description}</p>
                    ) : null}
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button variant="outline" onClick={() => startEdit(kb)}>
                      编辑
                    </Button>
                    <DangerousActionDialog
                      triggerLabel={`删除${kb.name ?? ""}`}
                      title="确认删除知识库"
                      description="删除后知识库及其资料将立即不可见，后台会继续清理派生数据。"
                      pending={remove.isPending}
                      onConfirm={() => onDelete(kb)}
                    />
                  </div>
                </div>
              </li>
            )
          )}
        </ul>
      )}

      <Pagination
        page={page}
        pageCount={totalPages}
        isFetching={list.isFetching}
        onPageChange={setPage}
        summary={`共 ${total} 个`}
      />
    </div>
  );
}
