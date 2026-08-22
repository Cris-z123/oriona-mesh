"use client";

import { ChevronDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { listKnowledgeBases } from "@/lib/api/client";
import type { KnowledgeBase } from "@/lib/api/types";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 100;
/** MVP 单用户规模上限：打开时按需加载全部有效知识库（最多 1000 个）。 */
const MAX_PAGES = 10;

/**
 * 知识库选择器（T157，ui-design §3.1）：打开时按需加载全部有效知识库，
 * 支持搜索过滤；未选择时显示明确选择态，绝不回退到任意第一个知识库。
 */
export function KnowledgeBasePicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (knowledgeBaseId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [loaded, setLoaded] = useState<KnowledgeBase[] | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  // 打开时按需加载全部页；仅异步完成后写入状态（避免 effect 内同步 setState）。
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void (async () => {
      const all: KnowledgeBase[] = [];
      for (let page = 1; page <= MAX_PAGES; page++) {
        const result = await listKnowledgeBases(page, PAGE_SIZE);
        if (cancelled) return;
        all.push(...result.items);
        if (all.length >= result.total) break;
      }
      if (!cancelled) setLoaded(all);
    })();
    return () => {
      cancelled = true;
    };
  }, [open]);

  // 点击菜单外部关闭。
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  const visible =
    loaded === null
      ? []
      : loaded.filter(
          (item) =>
            item.status === "active" && (filter === "" || (item.name ?? "").includes(filter))
        );
  const selected = loaded?.find((item) => item.id === value);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="选择知识库"
        onClick={() => setOpen((current) => !current)}
        className="flex h-9 min-w-52 items-center justify-between gap-2 rounded-md border border-input bg-background px-3 text-sm hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className="truncate">{selected?.name ?? "请选择知识库"}</span>
        <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
      </button>
      {open ? (
        <div className="absolute z-50 mt-1 w-full min-w-64 rounded-md border bg-surface p-1 shadow-lg">
          <input
            aria-label="搜索知识库"
            className="mb-1 h-8 w-full rounded border border-input bg-background px-2 text-sm"
            placeholder="搜索知识库…"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <ul role="listbox" aria-label="知识库列表" className="max-h-64 overflow-y-auto">
            {loaded === null ? (
              <li className="px-2 py-1.5 text-sm text-muted-foreground">正在加载…</li>
            ) : visible.length === 0 ? (
              <li className="px-2 py-1.5 text-sm text-muted-foreground">暂无匹配知识库</li>
            ) : (
              visible.map((item) => (
                <li
                  key={item.id}
                  role="option"
                  aria-selected={item.id === value}
                  className={cn(
                    "cursor-pointer rounded px-2 py-1.5 text-sm hover:bg-accent",
                    item.id === value && "bg-accent"
                  )}
                  onClick={() => {
                    onChange(item.id);
                    setOpen(false);
                    setFilter("");
                  }}
                >
                  {item.name ?? "未命名知识库"}
                </li>
              ))
            )}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
