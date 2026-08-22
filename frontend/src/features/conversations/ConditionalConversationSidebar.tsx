"use client";

import { useSearchParams } from "next/navigation";

import { ConversationSidebar } from "./ConversationSidebar";

/**
 * 全局侧栏会话历史（T148/T151/T157，ui-design §3.1）：
 * 仅当位于对话路由且 URL 携带明确知识库时呈现；其他页面绝不显示，
 * 也绝不回退猜测“当前知识库”。以 knowledgeBase 为 key，切换知识库时
 * 重置会话页码、选中态与本地编辑状态。
 */
export function ConditionalConversationSidebar({ onNavigate }: { onNavigate?: () => void }) {
  const searchParams = useSearchParams();
  const knowledgeBaseId = searchParams.get("knowledgeBase");
  if (!knowledgeBaseId) return null;
  return <ConversationSidebar key={knowledgeBaseId} onNavigate={onNavigate} />;
}
