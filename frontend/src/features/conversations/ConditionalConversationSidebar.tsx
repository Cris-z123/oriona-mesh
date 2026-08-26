"use client";

import { ConversationSidebar } from "./ConversationSidebar";

/**
 * 对话路由全局会话历史侧栏（T148/T151/T157/T173，ui-design §3.1）：
 * 仅当位于对话路由时由上层壳层呈现；其他页面绝不显示。全局历史不依赖 URL
 * 知识库参数，也不以 knowledgeBase 为 key 重置页码——知识库只决定新建对话范围。
 */
export function ConditionalConversationSidebar({ onNavigate }: { onNavigate?: () => void }) {
  return <ConversationSidebar onNavigate={onNavigate} />;
}
