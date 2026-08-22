"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useTheme } from "next-themes";

import { useAuth } from "@/features/auth/AuthProvider";

/** 侧栏底部的账户菜单；账户操作不与工作区主导航混排。 */
export function AccountMenu({ onNavigate }: { onNavigate?: () => void }) {
  const { user, signOut } = useAuth();
  const { resolvedTheme, setTheme } = useTheme();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const initial = (user?.display_name?.trim() || user?.email || "用户").charAt(0).toUpperCase();
  const themeLabel = resolvedTheme === "dark" ? "切换到浅色主题" : "切换到深色主题";
  const close = () => {
    setOpen(false);
    window.setTimeout(() => triggerRef.current?.focus(), 0);
  };

  // 点击菜单外部关闭（触发按钮在容器内，不会误触发）。
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) close();
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        aria-label="账户菜单"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex h-9 w-9 items-center justify-center rounded-full bg-accent text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {initial}
      </button>
      {open ? (
        <div
          role="menu"
          onKeyDown={(event) => {
            if (event.key === "Escape") close();
          }}
          className="absolute bottom-11 left-0 z-50 grid min-w-40 gap-1 rounded-md border bg-surface p-1 shadow-lg"
        >
          <Link
            href="/profile"
            role="menuitem"
            onClick={onNavigate}
            className="rounded px-2 py-1.5 text-sm hover:bg-accent"
          >
            个人资料
          </Link>
          <button
            type="button"
            role="menuitem"
            className="rounded px-2 py-1.5 text-left text-sm hover:bg-accent"
            onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
          >
            {themeLabel}
          </button>
          <button
            type="button"
            role="menuitem"
            className="rounded px-2 py-1.5 text-left text-sm hover:bg-accent"
            onClick={() => {
              onNavigate?.();
              void signOut();
            }}
          >
            退出登录
          </button>
        </div>
      ) : null}
    </div>
  );
}
