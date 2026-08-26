"use client";

import { Library, MessageSquareText, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, type ComponentType } from "react";

import { ConditionalConversationSidebar } from "@/features/conversations/ConditionalConversationSidebar";
import { useUiStore } from "@/stores/ui-store";
import { cn } from "@/lib/utils";

import { AccountMenu } from "./AccountMenu";

interface NavItem {
  href: string;
  label: string;
  icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
}

/** 工作区导航项。 */
export const NAV_ITEMS: NavItem[] = [
  { href: "/knowledge-bases", label: "知识库", icon: Library },
  { href: "/conversations", label: "对话", icon: MessageSquareText },
];

/** 导航链接（桌面侧栏与移动端抽屉共用）；折叠不丢失可访问名称。 */
export function NavLinks({
  collapsed = false,
  onNavigate,
}: {
  collapsed?: boolean;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();

  return (
    <nav aria-label="工作区导航" className="flex-1 space-y-1 p-2">
      {NAV_ITEMS.map((item) => {
        const Icon = item.icon;
        const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
        return (
          <Link
            key={item.href}
            href={item.href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            aria-label={collapsed ? item.label : undefined}
            title={collapsed ? item.label : undefined}
            className={cn(
              "flex h-9 items-center gap-2 rounded-md px-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              active && "bg-accent text-foreground",
              collapsed && "justify-center px-0"
            )}
          >
            <Icon className="h-4 w-4 shrink-0" aria-hidden />
            {!collapsed ? <span>{item.label}</span> : null}
          </Link>
        );
      })}
    </nav>
  );
}

/**
 * 左侧固定全局侧栏（T137/T148/T157，ui-design §3.1）：品牌、折叠开关、
 * 知识库管理与对话导航、底部账户菜单。会话历史在任意对话路由出现，按当前用户
 * 全局范围加载（ConditionalConversationSidebar），其他页面绝不显示。
 * 折叠状态保存在 UI store，仅维护布局与本地 UI 状态。
 */
export function WorkspaceNav() {
  const collapsed = useUiStore((state) => state.navCollapsed);
  const toggleCollapsed = useUiStore((state) => state.toggleNavCollapsed);
  const pathname = usePathname();
  const isConversationRoute =
    pathname === "/conversations" || pathname.startsWith("/conversations/");

  return (
    <aside
      aria-label="工作区导航侧栏"
      className={cn(
        "fixed inset-y-0 left-0 z-30 hidden flex-col border-r bg-surface transition-[width] duration-200 motion-reduce:transition-none lg:flex",
        collapsed ? "w-16" : "w-60"
      )}
    >
      <div
        className={cn(
          "flex h-14 shrink-0 items-center border-b px-3",
          collapsed ? "justify-center" : "justify-between"
        )}
      >
        {!collapsed ? (
          <Link
            href="/"
            aria-label="OrionaMesh 首页"
            className="flex min-w-0 items-center gap-2 rounded-md px-1 font-display text-lg font-semibold text-foreground hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span className="truncate">OrionaMesh</span>
          </Link>
        ) : null}
        <button
          type="button"
          aria-label={collapsed ? "展开导航" : "折叠导航"}
          aria-expanded={!collapsed}
          onClick={toggleCollapsed}
          className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {collapsed ? (
            <PanelLeftOpen className="h-4 w-4" aria-hidden />
          ) : (
            <PanelLeftClose className="h-4 w-4" aria-hidden />
          )}
        </button>
      </div>

      {collapsed ? (
        <NavLinks collapsed />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <NavLinks collapsed={false} />
          {isConversationRoute ? (
            <Suspense fallback={null}>
              <ConditionalConversationSidebar />
            </Suspense>
          ) : null}
        </div>
      )}

      <div className={cn("space-y-1 border-t p-2", collapsed && "flex flex-col items-center")}>
        <AccountMenu />
      </div>
    </aside>
  );
}
