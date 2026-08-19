"use client";

import type { ReactNode } from "react";

/**
 * 右侧上下文栏（T137，ui-design §3.1）：资料处理概况、当前会话上下文等
 * 非关键辅助信息。桌面（xl+）为固定侧栏；小视口下同一内容移入页面区域，
 * 不依赖隐藏控件降级。
 */
export function ContextRail({ children }: { children: ReactNode }) {
  return (
    <aside
      aria-label="上下文栏"
      className="mt-6 border-t pt-4 xl:fixed xl:inset-y-0 xl:right-0 xl:mt-0 xl:w-72 xl:border-l xl:border-t-0 xl:bg-surface xl:pt-0"
    >
      <div className="xl:h-full xl:overflow-y-auto xl:p-4">{children}</div>
    </aside>
  );
}
