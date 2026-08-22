"use client";

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

interface ToastContextValue {
  /** 短暂非阻塞成功反馈（T160，ui-design §5）：自动消失，不打断当前工作。 */
  notify: (message: string) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const TOAST_DURATION_MS = 2_500;

/** 全局短暂成功反馈容器：右下角浮层，aria-live 公告。 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  const notify = useCallback((text: string) => {
    setMessage(text);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setMessage(null), TOAST_DURATION_MS);
  }, []);

  return (
    <ToastContext.Provider value={{ notify }}>
      {children}
      {message ? (
        <div
          role="status"
          aria-live="polite"
          className="fixed bottom-6 right-6 z-50 rounded-md border bg-surface px-4 py-2 text-sm text-foreground shadow-lg"
        >
          {message}
        </div>
      ) : null}
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast 必须在 ToastProvider 内使用");
  return ctx;
}
