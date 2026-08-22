"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { ErrorState, toErrorStateValue } from "@/components/ui/error-state";
import { useAuth } from "@/features/auth/AuthProvider";

/** 受保护路由守卫：未登录重定向到 /login；会话恢复期间不渲染内容避免闪烁。 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { ready, session, recoveryError, retryRecovery, signOut } = useAuth();
  const router = useRouter();
  const [exiting, setExiting] = useState(false);

  useEffect(() => {
    if (ready && !session) router.replace("/login");
  }, [ready, session, router]);

  if (exiting) return null;
  if (recoveryError && session) {
    return (
      <div className="mx-auto max-w-lg space-y-3 p-6">
        <ErrorState
          error={toErrorStateValue(recoveryError, "恢复会话失败，请重试或退出登录。")}
          onRetry={retryRecovery}
        />
        <Button
          variant="outline"
          onClick={() => {
            setExiting(true);
            // signOut 先撤销服务端会话（携带 Bearer + refresh token），finally 再清本地会话；
            // 不得先 clearSession，否则服务端撤销请求读不到令牌而静默失败。
            void signOut().finally(() => router.replace("/login"));
          }}
        >
          退出登录
        </Button>
      </div>
    );
  }
  if (!ready || !session) return null;
  return children;
}
