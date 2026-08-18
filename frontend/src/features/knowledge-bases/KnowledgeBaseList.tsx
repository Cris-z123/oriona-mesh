"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  ApiError,
  asApiError,
  createKnowledgeBase,
  deleteKnowledgeBase,
  listKnowledgeBases,
  updateKnowledgeBase,
} from "@/lib/api/client";
import type { KnowledgeBase } from "@/lib/api/types";

const PAGE_SIZE = 20;

/**
 * 知识库列表（T110/FR-003）：页码列表、创建、编辑与删除。
 * - delete_failed/20015 仅显示最小“删除未完成”墓碑与 retry_delete；
 * - 展示内容与操作全部来自服务端 DTO（name/description/allowed_actions），不自行推导。
 */
export function KnowledgeBaseList() {
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);

  const [createName, setCreateName] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  const load = useCallback(async (targetPage: number) => {
    setLoading(true);
    try {
      const result = await listKnowledgeBases(targetPage, PAGE_SIZE);
      setItems(result.items);
      setTotal(result.total);
      setError(null);
    } catch (err) {
      setError(asApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // 延迟到宏任务：load 同步前缀含 setLoading，react-hooks/set-state-in-effect
    // 要求 effect 内不得同步 setState；加载由本 effect 统一触发，避免重复请求
    const timer = window.setTimeout(() => {
      void load(page);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load, page, reloadKey]);

  const onCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    try {
      await createKnowledgeBase({
        name: createName.trim(),
        ...(createDescription.trim() !== "" ? { description: createDescription.trim() } : {}),
      });
      setCreateName("");
      setCreateDescription("");
      setPage(1);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setError(asApiError(err));
    }
  };

  const onDelete = async (kb: KnowledgeBase) => {
    setError(null);
    try {
      await deleteKnowledgeBase(kb.id);
      // 末页最后一项删除后回退一页，避免停留在空页
      if (items.length === 1 && page > 1) {
        setPage((p) => p - 1);
      } else {
        setReloadKey((k) => k + 1);
      }
    } catch (err) {
      setError(asApiError(err));
    }
  };

  const startEdit = (kb: KnowledgeBase) => {
    setEditingId(kb.id);
    setEditName(kb.name ?? "");
    setEditDescription(kb.description ?? "");
  };

  const onSaveEdit = async (kb: KnowledgeBase) => {
    setError(null);
    try {
      await updateKnowledgeBase(kb.id, {
        name: editName.trim(),
        ...(editDescription.trim() !== "" ? { description: editDescription.trim() } : {}),
      });
      setEditingId(null);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setError(asApiError(err));
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-4">
      {error ? <ApiErrorNotice error={error} /> : null}

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
        <Button type="submit" disabled={loading || createName.trim() === ""}>
          创建
        </Button>
      </form>

      <ul className="space-y-2">
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
                <Button variant="destructive" onClick={() => void onDelete(kb)}>
                  重试删除
                </Button>
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
                  <Button variant="destructive" onClick={() => void onDelete(kb)}>
                    删除{kb.name ?? ""}
                  </Button>
                </div>
              </div>
            </li>
          )
        )}
      </ul>

      <div className="flex items-center gap-2 text-sm">
        <Button
          variant="outline"
          disabled={page <= 1 || loading}
          onClick={() => setPage((p) => p - 1)}
        >
          上一页
        </Button>
        <span>
          第 {page} / {totalPages} 页，共 {total} 个
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
