"use client";

import { useQueryClient } from "@tanstack/react-query";
import { LibraryBig, MoreHorizontal, Plus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { DangerousActionDialog } from "@/components/ui/dangerous-action-dialog";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Pagination, pageAfterDeletingLastItem } from "@/components/ui/pagination";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
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
  const router = useRouter();
  const { notify } = useToast();
  const deletingIdsRef = useRef(new Set<string>());
  const [page, setPage] = useState(1);

  const [createName, setCreateName] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [pendingDelete, setPendingDelete] = useState<KnowledgeBase | null>(null);

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
      const created = await create.mutateAsync({
        name: createName.trim(),
        ...(createDescription.trim() !== "" ? { description: createDescription.trim() } : {}),
      });
      // 创建成功后直接进入该知识库的资料工作区（T149）；本组件随后卸载，
      // 重取仅覆盖导航过渡窗口内仍挂载的列表（与删除/改名后的写后重取一致）。
      notify("知识库已创建");
      setCreateDialogOpen(false);
      router.push(`/knowledge-bases/${created.id}`);
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
          description: editDescription.trim(),
        },
      });
      setEditingId(null);
      await refreshList();
      notify("已保存");
    } catch {
      // 错误已由 mutation.error 呈现
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-5">
      {error ? <ErrorState error={error} /> : null}

      <header className="flex items-center justify-between gap-4 border-b pb-4">
        <div>
          <h1 className="font-display text-2xl font-semibold">知识库</h1>
          <p className="mt-1 text-sm text-muted-foreground">建立并维护私有知识库</p>
        </div>
        <Button onClick={() => setCreateDialogOpen(true)}>
          <Plus className="h-4 w-4" aria-hidden />
          新建知识库
        </Button>
      </header>

      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent aria-label="新建知识库" className="space-y-5">
          <div className="space-y-1.5">
            <DialogTitle className="font-display text-xl">新建知识库</DialogTitle>
            <DialogDescription>为一组相互关联的资料建立独立工作区。</DialogDescription>
          </div>
          <form onSubmit={onCreate} className="space-y-4">
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
            <div className="flex justify-end gap-2 pt-2">
              <Button variant="ghost" onClick={() => setCreateDialogOpen(false)}>
                取消
              </Button>
              <Button type="submit" disabled={create.isPending || createName.trim() === ""}>
                创建并进入资料
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>

      {list.isLoading ? (
        <ul className="space-y-2" aria-label="加载中">
          {[0, 1, 2].map((i) => (
            <li key={i}>
              <Skeleton className="h-14 w-full" />
            </li>
          ))}
        </ul>
      ) : items.length === 0 ? (
        <EmptyState
          icon={<LibraryBig className="h-6 w-6" aria-hidden />}
          title="暂无知识库"
          description="创建第一个知识库，开始整理可追溯的资料。"
          action={
            <Button variant="outline" onClick={() => setCreateDialogOpen(true)}>
              <Plus className="h-4 w-4" aria-hidden />
              新建知识库
            </Button>
          }
        />
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
              <li key={kb.id} className="rounded-md border bg-surface px-4 py-3">
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
                    <Link
                      href={`/knowledge-bases/${kb.id}`}
                      className="block truncate rounded font-medium hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      aria-label={`打开资料 ${kb.name ?? ""}`}
                      title={`打开资料 ${kb.name ?? ""}`}
                    >
                      {kb.name}
                    </Link>
                    {kb.description ? (
                      <p className="truncate text-sm text-muted-foreground">{kb.description}</p>
                    ) : null}
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          className="h-9 w-9 p-0"
                          aria-label={`知识库操作 ${kb.name ?? ""}`}
                          title={`知识库操作 ${kb.name ?? ""}`}
                        >
                          <MoreHorizontal className="h-4 w-4" aria-hidden />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent>
                        <DropdownMenuItem onSelect={() => startEdit(kb)}>编辑</DropdownMenuItem>
                        <DropdownMenuItem onSelect={() => setPendingDelete(kb)}>
                          删除
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
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

      <AlertDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogTitle>确认删除知识库</AlertDialogTitle>
          <AlertDialogDescription>
            删除后知识库及其资料将立即不可见，后台会继续清理派生数据。
          </AlertDialogDescription>
          <div className="mt-5 flex justify-end gap-2">
            <AlertDialogCancel asChild>
              <Button variant="outline" disabled={remove.isPending}>
                取消
              </Button>
            </AlertDialogCancel>
            <AlertDialogAction asChild>
              <Button
                variant="destructive"
                disabled={remove.isPending}
                onClick={() => {
                  if (pendingDelete) void onDelete(pendingDelete);
                  setPendingDelete(null);
                }}
              >
                确认删除
              </Button>
            </AlertDialogAction>
          </div>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
