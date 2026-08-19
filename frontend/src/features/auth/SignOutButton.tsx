"use client";

import { LogOut } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/features/auth/AuthProvider";

/**
 * 退出登录（FR-001）：撤销服务端会话并清除本地会话。
 * 重定向由 RequireAuth 守卫在会话清空后统一处理。
 * `iconOnly`：折叠导航等紧凑场景只保留可访问名称（aria-label）。
 */
export function SignOutButton({ iconOnly = false }: { iconOnly?: boolean }) {
  const { signOut } = useAuth();
  const [busy, setBusy] = useState(false);

  const onClick = async () => {
    setBusy(true);
    try {
      await signOut();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Button
      variant="outline"
      disabled={busy}
      onClick={() => void onClick()}
      aria-label={iconOnly ? "退出登录" : undefined}
      title={iconOnly ? "退出登录" : undefined}
    >
      {iconOnly ? <LogOut className="h-4 w-4" aria-hidden /> : "退出登录"}
    </Button>
  );
}
