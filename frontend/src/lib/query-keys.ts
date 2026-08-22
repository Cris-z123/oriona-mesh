/**
 * TanStack Query 资源键（ui-design §6.1）：按服务端资源层级集中维护，
 * 避免知识库和资料功能各自复制前缀而导致失效范围漂移。
 */
import type { DocumentStatusFilter } from "@/lib/api/types";

export const queryKeys = {
  knowledgeBases: (page: number, pageSize: number) =>
    ["knowledgeBases", { page, pageSize }] as const,
  knowledgeBaseDetail: (knowledgeBaseId: string) => ["knowledgeBases", knowledgeBaseId] as const,
  knowledgeBasesAll: () => ["knowledgeBases"] as const,
  documents: (knowledgeBaseId: string) => ["knowledgeBases", knowledgeBaseId, "documents"] as const,
  documentList: (
    knowledgeBaseId: string,
    page: number,
    pageSize: number,
    status: DocumentStatusFilter
  ) => [...queryKeys.documents(knowledgeBaseId), { page, pageSize, status }] as const,
  documentDetail: (knowledgeBaseId: string, documentId: string) =>
    [...queryKeys.documents(knowledgeBaseId), documentId] as const,
  documentTasks: (knowledgeBaseId: string, documentId: string, page: number, pageSize: number) =>
    [
      ...queryKeys.documentDetail(knowledgeBaseId, documentId),
      "tasks",
      { page, pageSize },
    ] as const,
  conversations: (knowledgeBaseId: string, page: number, pageSize: number) =>
    ["conversations", knowledgeBaseId, { page, pageSize }] as const,
  conversationsAll: () => ["conversations"] as const,
  conversationDetail: (conversationId: string) => ["conversations", conversationId] as const,
  messagesAll: (conversationId: string) => ["conversations", conversationId, "messages"] as const,
  citationsAll: (conversationId: string, messageId: string) =>
    ["conversations", conversationId, "messages", messageId, "citations"] as const,
};
