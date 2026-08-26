"use client";

import { Menu } from "lucide-react";
import { usePathname } from "next/navigation";
import { Suspense, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { ToastProvider } from "@/components/ui/toast";
import { ConditionalConversationSidebar } from "@/features/conversations/ConditionalConversationSidebar";
import { useUiStore } from "@/stores/ui-store";
import { cn } from "@/lib/utils";

import { AccountMenu } from "./AccountMenu";
import { NavLinks, WorkspaceNav } from "./WorkspaceNav";

/**
 * 桌面工作台应用壳（T137/T148 修订，ui-design §3.1）：
 * 左侧固定全局侧栏（lg+，可折叠，仅品牌/导航/账户）＋中央主工作区。
 * 小于 lg 时导航移入可访问抽屉，抽屉保留导航与账户等价入口。
 * 对话路由的全局会话历史由壳层组合，不依赖 URL 中是否存在知识库参数（T157/T173）。
 * 本组件只维护布局与本地 UI 状态，不获取或改写业务真相。
 */
export function AppShell({ children }: { children: ReactNode }) {
  const collapsed = useUiStore((state) => state.navCollapsed);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const pathname = usePathname();
  const isConversationRoute =
    pathname === "/conversations" || pathname.startsWith("/conversations/");

  return (
    <ToastProvider>
      <div className="min-h-screen">
        {/* 小视口顶栏：导航抽屉入口 */}
        <header className="flex h-14 items-center gap-2 border-b bg-surface px-3 lg:hidden">
          <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
            <SheetTrigger asChild>
              <Button variant="ghost" aria-label="打开导航" className="px-2">
                <Menu className="h-5 w-5" aria-hidden />
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="w-72 sm:max-w-sm" aria-label="OrionaMesh">
              <SheetTitle className="font-display">OrionaMesh</SheetTitle>
              <NavLinks onNavigate={() => setMobileNavOpen(false)} />
              {isConversationRoute ? (
                <div className="min-h-0 flex-1 overflow-y-auto">
                  <Suspense fallback={null}>
                    <ConditionalConversationSidebar onNavigate={() => setMobileNavOpen(false)} />
                  </Suspense>
                </div>
              ) : null}
              <div className="mt-auto border-t pt-3">
                <AccountMenu onNavigate={() => setMobileNavOpen(false)} portalled={false} />
              </div>
            </SheetContent>
          </Sheet>
          <span className="font-display text-lg font-semibold">OrionaMesh</span>
        </header>

        {/* 桌面固定导航 */}
        <WorkspaceNav />

        {/* 中央主工作区 */}
        <div
          className={cn(
            "pt-14 transition-[padding] duration-200 motion-reduce:transition-none lg:pt-0",
            collapsed ? "lg:pl-16" : "lg:pl-60"
          )}
        >
          <main className="mx-auto w-full max-w-5xl px-4 py-6 lg:py-8">{children}</main>
        </div>
      </div>
    </ToastProvider>
  );
}
