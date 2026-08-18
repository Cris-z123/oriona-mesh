"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/features/auth/AuthProvider";

/** 退出登录（FR-001）：撤销服务端会话并清除本地会话后返回登录页。 */
export function SignOutButton() {
  const { signOut } = useAuth();
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  const onClick = async () => {
    setBusy(true);
    try {
      await signOut();
    } finally {
      setBusy(false);
    }
    router.replace("/login");
  };

  return (
    <Button variant="outline" disabled={busy} onClick={() => void onClick()}>
      退出登录
    </Button>
  );
}
