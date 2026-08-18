"use client";

import { useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { useAuth } from "@/features/auth/AuthProvider";

/** 受保护路由守卫：未登录重定向到 /login；会话恢复期间不渲染内容避免闪烁。 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { ready, session } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (ready && !session) router.replace("/login");
  }, [ready, session, router]);

  if (!ready || !session) return null;
  return children;
}
