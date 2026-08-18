"use client";

import type { ReactNode } from "react";

/**
 * 右侧上下文栏（T137，ui-design §3.1）：资料处理概况、当前会话上下文等
 * 非关键辅助信息。桌面（xl+）为固定侧栏；小视口下同一内容移入页面区域，
 * 不依赖隐藏控件降级。
 */
export function ContextRail({ children }: { children: ReactNode }) {
  return (
    <>
      <aside
        aria-label="上下文栏"
        className="fixed inset-y-0 right-0 hidden w-72 border-l bg-surface xl:block"
      >
        <div className="h-full overflow-y-auto p-4">{children}</div>
      </aside>
      <div className="mt-6 border-t pt-4 xl:hidden">{children}</div>
    </>
  );
}
