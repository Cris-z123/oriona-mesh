"use client";

import * as DropdownMenuPrimitive from "@radix-ui/react-dropdown-menu";
import { cn } from "@/lib/utils";

/** 列表行操作菜单（Radix DropdownMenu）：会话重命名/删除等次要操作收进图标按钮。 */
export const DropdownMenu = DropdownMenuPrimitive.Root;
export const DropdownMenuTrigger = DropdownMenuPrimitive.Trigger;

export function DropdownMenuContent({
  className,
  portalled = true,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Content> & { portalled?: boolean }) {
  const content = (
    <DropdownMenuPrimitive.Content
      align="end"
      className={cn("z-50 min-w-32 rounded-md border bg-surface p-1 shadow-lg", className)}
      {...props}
    />
  );

  return portalled ? (
    <DropdownMenuPrimitive.Portal>{content}</DropdownMenuPrimitive.Portal>
  ) : (
    content
  );
}

export function DropdownMenuItem({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Item>) {
  return (
    <DropdownMenuPrimitive.Item
      className={cn(
        "flex cursor-pointer items-center rounded px-2 py-1.5 text-sm text-foreground outline-none hover:bg-accent hover:text-accent-foreground focus-visible:ring-2 focus-visible:ring-ring",
        className
      )}
      {...props}
    />
  );
}
