import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type Query,
} from "@tanstack/react-query";

import { ApiError, deleteDocument, getDocument, listDocuments } from "@/lib/api/client";
import type { Document, Page } from "@/lib/api/types";
import { isInFlight } from "@/features/documents/status";
import type { DocumentStatusFilter } from "@/stores/ui-store";

/**
 * 资料查询封装（T138，ui-design §6.1/6.2）：
 * - 查询键按资源层级构造（knowledgeBases → documents → 列表/详情）；
 * - DTO 驱动的非终态轮询：仅在数据仍为非终态时按间隔重取，终态后停止；
 * - mutation 成功后只精确失效该知识库的资料子树（refetchType "none"，
 *   不重取），由组件在需要时显式重取，避免“末页删除回退”出现重复请求。
 */

export const queryKeys = {
  knowledgeBases: (page: number, pageSize: number) =>
    ["knowledgeBases", { page, pageSize }] as const,
  /** 某知识库的资料子树前缀（列表与详情）。 */
  documents: (knowledgeBaseId: string) => ["knowledgeBases", knowledgeBaseId, "documents"] as const,
  documentList: (
    knowledgeBaseId: string,
    page: number,
    pageSize: number,
    status: DocumentStatusFilter
  ) => [...queryKeys.documents(knowledgeBaseId), { page, pageSize, status }] as const,
  documentDetail: (knowledgeBaseId: string, documentId: string) =>
    [...queryKeys.documents(knowledgeBaseId), documentId] as const,
};

export interface UseDocumentListOptions {
  /** 非终态资料轮询间隔（毫秒）。 */
  pollIntervalMs?: number;
}

const DEFAULT_POLL_INTERVAL_MS = 3_000;

/** 资料列表：DTO 驱动轮询，仅当存在非终态资料时继续重取。 */
export function useDocumentList(
  knowledgeBaseId: string,
  page: number,
  pageSize: number,
  status: DocumentStatusFilter,
  options: UseDocumentListOptions = {}
) {
  const pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
  return useQuery<Page<Document>, ApiError>({
    queryKey: queryKeys.documentList(knowledgeBaseId, page, pageSize, status),
    // 翻页期间保留上一页数据，避免列表闪空（与阶段 8 手动加载行为一致）
    placeholderData: keepPreviousData,
    queryFn: () =>
      listDocuments(knowledgeBaseId, page, pageSize, status === "all" ? undefined : status),
    refetchInterval: (query: Query<Page<Document>, ApiError>) => {
      const items = query.state.data?.items;
      if (!items || !items.some((doc) => isInFlight(doc.status))) return false;
      return pollIntervalMs;
    },
  });
}

/** 资料详情：DTO 驱动轮询，终态或尚未加载时停止。 */
export function useDocumentDetail(
  knowledgeBaseId: string,
  documentId: string,
  options: UseDocumentListOptions = {}
) {
  const pollIntervalMs = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
  return useQuery<Document, ApiError>({
    queryKey: queryKeys.documentDetail(knowledgeBaseId, documentId),
    queryFn: () => getDocument(knowledgeBaseId, documentId),
    refetchInterval: (query: Query<Document, ApiError>) => {
      const doc = query.state.data;
      if (!doc || !isInFlight(doc.status)) return false;
      return pollIntervalMs;
    },
  });
}

/** 删除资料：成功后精确失效该知识库的资料子树（列表与详情）。 */
export function useDeleteDocument() {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, { knowledgeBaseId: string; documentId: string }>({
    mutationFn: ({ knowledgeBaseId, documentId }) => deleteDocument(knowledgeBaseId, documentId),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.documents(variables.knowledgeBaseId),
        refetchType: "none",
      });
    },
  });
}
