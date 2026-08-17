import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "OrionaMesh",
  description: "私有知识库 RAG 问答",
};

/**
 * 根布局：阶段 7 只建立骨架（无业务渲染，前端 UI 任务自阶段 8 起）。
 * 认证/会话等业务能力在 `src/features/` 中实现，不在根布局复制后端规则。
 */
export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
