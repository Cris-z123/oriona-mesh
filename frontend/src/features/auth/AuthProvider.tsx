"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import { ApiError, getMe, logout as apiLogout } from "@/lib/api/client";
import { clearSession, getSession, subscribeSession, type SessionState } from "@/lib/api/session";
import type { User } from "@/lib/api/types";

interface AuthContextValue {
  session: SessionState | null;
  user: User | null;
  /** 会话恢复完成（localStorage 检查 + /users/me 拉取）后为 true。 */
  ready: boolean;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** SSR/水合阶段快照：服务端始终无会话，客户端首个渲染再由 getSnapshot 读取。 */
function getServerSnapshot(): SessionState | null {
  return null;
}

/**
 * 认证上下文（T109）：会话状态通过 useSyncExternalStore 订阅 session 存储；
 * 恢复时拉取 /users/me，Access Token 失效由客户端自动轮换，10001 轮换失败则清除会话。
 * ready 与 user 均由渲染期派生，不在 effect 中同步 setState。
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const session = useSyncExternalStore(subscribeSession, getSession, getServerSnapshot);

  const [meResult, setMeResult] = useState<
    { status: "pending" } | { status: "done"; user: User | null }
  >({ status: "pending" });

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    getMe()
      .then((me) => {
        if (!cancelled) setMeResult({ status: "done", user: me });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // 会话已过期且无法恢复：清除本地会话，订阅回调会触发重定向
        if (err instanceof ApiError && err.code === 10001) clearSession();
        setMeResult({ status: "done", user: null });
      });
    return () => {
      cancelled = true;
    };
  }, [session]);

  const user = session && meResult.status === "done" ? meResult.user : null;
  const ready = session ? meResult.status === "done" : true;

  const signOut = useCallback(async () => {
    await apiLogout();
  }, []);

  const value = useMemo(() => ({ session, user, ready, signOut }), [session, user, ready, signOut]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return ctx;
}
