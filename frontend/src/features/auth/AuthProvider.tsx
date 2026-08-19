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
  updateCurrentUser: (user: User) => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** SSR/水合阶段快照：服务端始终无会话，客户端首个渲染再由 getSnapshot 读取。 */
function getServerSnapshot(): SessionState | null {
  return null;
}

/** 水合前必须维持 false，首个客户端快照则为 true，避免受保护路由抢先重定向。 */
function subscribeHydration(): () => void {
  return () => undefined;
}

function getClientHydrationSnapshot(): boolean {
  return true;
}

function getServerHydrationSnapshot(): boolean {
  return false;
}

/**
 * 认证上下文（T109）：会话状态通过 useSyncExternalStore 订阅 session 存储；
 * 恢复时拉取 /users/me，Access Token 失效由客户端自动轮换，10001 轮换失败则清除会话。
 * ready 与 user 均由渲染期派生，不在 effect 中同步 setState。
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const session = useSyncExternalStore(subscribeSession, getSession, getServerSnapshot);
  const sessionKey = session ? `${session.accessToken}:${session.refreshToken}` : null;
  // SSR 快照必为 null；在客户端首次同步真实 localStorage 前不得让受保护路由重定向。
  const hydrated = useSyncExternalStore(
    subscribeHydration,
    getClientHydrationSnapshot,
    getServerHydrationSnapshot
  );

  const [meResult, setMeResult] = useState<{
    sessionKey: string | null;
    status: "pending" | "done";
    user: User | null;
  }>({ sessionKey: null, status: "done", user: null });

  useEffect(() => {
    if (!session || !sessionKey) return;

    let cancelled = false;
    let retryTimer: number | undefined;
    let retries = 0;
    const attempt = () => {
      getMe()
        .then((me) => {
          if (!cancelled) setMeResult({ sessionKey, status: "done", user: me });
        })
        .catch((err: unknown) => {
          if (cancelled) return;
          if (err instanceof ApiError && err.code === 10001) {
            // 会话已过期且无法恢复：清除本地会话，订阅回调会触发重定向
            clearSession();
            setMeResult({ sessionKey, status: "done", user: null });
            return;
          }
          // 瞬时错误：有界延迟重试，避免把有效会话误判为未登录
          retries += 1;
          if (retries <= 2) {
            retryTimer = window.setTimeout(attempt, 2000);
          } else {
            setMeResult({ sessionKey, status: "done", user: null });
          }
        });
    };
    attempt();
    return () => {
      cancelled = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [session, sessionKey]);

  const isCurrentSession = sessionKey !== null && meResult.sessionKey === sessionKey;
  const user = isCurrentSession && meResult.status === "done" ? meResult.user : null;
  const ready = hydrated && (session ? isCurrentSession && meResult.status === "done" : true);

  const signOut = useCallback(async () => {
    await apiLogout();
  }, []);

  const updateCurrentUser = useCallback(
    (updated: User) => {
      if (!sessionKey) return;
      setMeResult((current) =>
        current.sessionKey === sessionKey ? { sessionKey, status: "done", user: updated } : current
      );
    },
    [sessionKey]
  );

  const value = useMemo(
    () => ({ session, user, ready, signOut, updateCurrentUser }),
    [session, user, ready, signOut, updateCurrentUser]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return ctx;
}
