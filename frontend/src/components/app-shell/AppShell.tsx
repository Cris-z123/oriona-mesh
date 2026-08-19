"use client";

import { Menu } from "lucide-react";
import { useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { useUiStore } from "@/stores/ui-store";
import { cn } from "@/lib/utils";

import { ContextRail } from "./ContextRail";
import { NavLinks, WorkspaceNav } from "./WorkspaceNav";

/**
 * 桌面工作台应用壳（T137，ui-design §3.1）：
 * 左侧固定工作区导航（lg+，可折叠）＋中央主工作区＋右侧可选上下文栏（xl+）。
 * 小于 lg 时导航移入可访问抽屉；小于 xl 时上下文栏内容移入页面区域。
 * 本组件只维护布局与本地 UI 状态，不获取或改写业务真相。
 */
export function AppShell({
  children,
  contextRail,
}: {
  children: ReactNode;
  contextRail?: ReactNode;
}) {
  const collapsed = useUiStore((state) => state.navCollapsed);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  return (
    <div className="min-h-screen">
      {/* 小视口顶栏：导航抽屉入口 */}
      <header className="flex h-14 items-center gap-2 border-b bg-surface px-3 lg:hidden">
        <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
          <SheetTrigger asChild>
            <Button variant="ghost" aria-label="打开导航" className="px-2">
              <Menu className="h-5 w-5" aria-hidden />
            </Button>
          </SheetTrigger>
          <SheetContent side="left" className="w-64">
            <SheetTitle className="font-display">OrionaMesh</SheetTitle>
            <NavLinks onNavigate={() => setMobileNavOpen(false)} />
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
          collapsed ? "lg:pl-16" : "lg:pl-60",
          contextRail ? "xl:pr-72" : ""
        )}
      >
        <main className="mx-auto w-full max-w-5xl px-4 py-6 lg:py-8">{children}</main>
        {contextRail ? <ContextRail>{contextRail}</ContextRail> : null}
      </div>
    </div>
  );
}
