"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/features/auth/AuthProvider";

/**
 * 退出登录（FR-001）：撤销服务端会话并清除本地会话。
 * 重定向由 RequireAuth 守卫在会话清空后统一处理。
 */
export function SignOutButton() {
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
    <Button variant="outline" disabled={busy} onClick={() => void onClick()}>
      退出登录
    </Button>
  );
}
