import type { Metadata } from "next";
import { ThemeProvider } from "next-themes";

import { AuthProvider } from "@/features/auth/AuthProvider";
import { QueryProvider } from "@/lib/query-client";
import "./globals.css";

export const metadata: Metadata = {
  title: "OrionaMesh",
  description: "私有知识库 RAG 问答",
};

/**
 * 根布局（T136）：QueryProvider 管理全部服务器状态；ThemeProvider 负责
 * 浅色默认与“夜间编辑桌”深色（next-themes，class 策略）；AuthProvider 提供认证上下文。
 * 业务规则与接口细节保留在 `src/features/` 与 `src/lib/api/`，不在布局中复制。
 */
export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>
        <QueryProvider>
          <ThemeProvider
            attribute="class"
            defaultTheme="light"
            enableSystem
            disableTransitionOnChange
          >
            <AuthProvider>{children}</AuthProvider>
          </ThemeProvider>
        </QueryProvider>
      </body>
    </html>
  );
}
