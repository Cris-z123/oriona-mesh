import type { Metadata } from "next";

import { AuthProvider } from "@/features/auth/AuthProvider";
import "./globals.css";

export const metadata: Metadata = {
  title: "OrionaMesh",
  description: "私有知识库 RAG 问答",
};

/**
 * 根布局：阶段 8 起提供认证上下文（会话恢复/用户拉取）。
 * 业务规则与接口细节保留在 `src/features/` 与 `src/lib/api/`，不在布局中复制。
 */
export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
