/**
 * 会话存储（T108/T109）：Access/Refresh Token 仅保存在浏览器 localStorage，
 * 前端从不接触数据库、队列或文件存储。Refresh Token 明文只用于 PUT /auth/sessions
 * 轮换与登出请求体（openapi 契约），不允许写入日志。
 */

export interface SessionState {
  accessToken: string;
  refreshToken: string;
  /** Access Token 到期时刻（毫秒时间戳）；由服务端 expires_in=7200 计算。 */
  expiresAt: number;
}

const STORAGE_KEY = "orionamesh.session.v1";

let current: SessionState | null = null;
let initialized = false;
const listeners = new Set<(session: SessionState | null) => void>();

function load(): SessionState | null {
  if (initialized) return current;
  initialized = true;
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<SessionState>;
    if (
      typeof parsed.accessToken === "string" &&
      typeof parsed.refreshToken === "string" &&
      typeof parsed.expiresAt === "number"
    ) {
      current = parsed as SessionState;
      return current;
    }
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // localStorage 不可用（隐私模式等）时按未登录处理
  }
  return null;
}

export function getSession(): SessionState | null {
  return load();
}

export function setSession(session: SessionState): void {
  current = session;
  initialized = true;
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    } catch {
      // 忽略持久化失败；本次会话仍可用
    }
  }
  for (const listener of listeners) listener(session);
}

export function clearSession(): void {
  current = null;
  initialized = true;
  if (typeof window !== "undefined") {
    try {
      window.localStorage.removeItem(STORAGE_KEY);
    } catch {
      // 忽略清理失败
    }
  }
  for (const listener of listeners) listener(null);
}

/** 订阅会话变化（AuthProvider 使用）；返回取消订阅函数。 */
export function subscribeSession(listener: (session: SessionState | null) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
