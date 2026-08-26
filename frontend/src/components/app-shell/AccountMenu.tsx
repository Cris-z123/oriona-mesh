"use client";

import { LogOut, Moon, UserRound } from "lucide-react";
import Link from "next/link";
import { useTheme } from "next-themes";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuth } from "@/features/auth/AuthProvider";

/** 侧栏底部的账户菜单；以 Radix 处理键盘导航、焦点回归与点击外部关闭。 */
export function AccountMenu({
  onNavigate,
  portalled = true,
}: {
  onNavigate?: () => void;
  /** 模态抽屉内禁用 Portal，保持菜单处于当前焦点层。 */
  portalled?: boolean;
}) {
  const { user, signOut } = useAuth();
  const { resolvedTheme, setTheme } = useTheme();
  const initial = (user?.display_name?.trim() || user?.email || "用户").charAt(0).toUpperCase();
  const themeLabel = resolvedTheme === "dark" ? "切换到浅色主题" : "切换到深色主题";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label="账户菜单"
          className="flex h-9 w-9 items-center justify-center rounded-full bg-accent text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {initial}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent portalled={portalled} side="top" align="start" className="min-w-44">
        <DropdownMenuItem asChild>
          <Link href="/profile" onClick={onNavigate}>
            <UserRound className="mr-2 h-4 w-4" aria-hidden />
            个人资料
          </Link>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}>
          <Moon className="mr-2 h-4 w-4" aria-hidden />
          {themeLabel}
        </DropdownMenuItem>
        <DropdownMenuItem
          className="text-destructive focus:bg-destructive/10 focus:text-destructive"
          onSelect={() => {
            onNavigate?.();
            void signOut();
          }}
        >
          <LogOut className="mr-2 h-4 w-4" aria-hidden />
          退出登录
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
