import type { HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

/** 加载骨架（shadcn/ui 风格；只表达“加载中”，不代替终态消息，ui-design §7）。 */
export function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("animate-pulse rounded-md bg-muted", className)} {...props} />;
}
