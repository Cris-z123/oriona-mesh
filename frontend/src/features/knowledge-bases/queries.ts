import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  createKnowledgeBase,
  deleteKnowledgeBase,
  listKnowledgeBases,
  updateKnowledgeBase,
} from "@/lib/api/client";
import type { KnowledgeBase, Page } from "@/lib/api/types";

/**
 * 知识库查询封装（T138，ui-design §6.1）：列表与增删改 mutation。
 * 写操作成功后精确失效整个知识库资源层级（refetchType "none" 只标记过期，
 * 由组件显式重取当前页），知识库无非终态状态、不需要轮询。
 */

export const knowledgeBaseQueryKeys = {
  list: (page: number, pageSize: number) => ["knowledgeBases", { page, pageSize }] as const,
  /** 知识库资源层级前缀（列表/详情/资料子树）。 */
  all: () => ["knowledgeBases"] as const,
};

export function useKnowledgeBaseList(page: number, pageSize = 20) {
  return useQuery<Page<KnowledgeBase>, ApiError>({
    queryKey: knowledgeBaseQueryKeys.list(page, pageSize),
    queryFn: () => listKnowledgeBases(page, pageSize),
  });
}

function useKnowledgeBaseInvalidator() {
  const queryClient = useQueryClient();
  return () => {
    queryClient.invalidateQueries({
      queryKey: knowledgeBaseQueryKeys.all(),
      refetchType: "none",
    });
  };
}

export function useCreateKnowledgeBase() {
  const invalidate = useKnowledgeBaseInvalidator();
  return useMutation<KnowledgeBase, ApiError, { name: string; description?: string }>({
    mutationFn: (input) => createKnowledgeBase(input),
    onSuccess: invalidate,
  });
}

export function useUpdateKnowledgeBase() {
  const invalidate = useKnowledgeBaseInvalidator();
  return useMutation<
    KnowledgeBase,
    ApiError,
    { id: string; input: { name?: string; description?: string } }
  >({
    mutationFn: ({ id, input }) => updateKnowledgeBase(id, input),
    onSuccess: invalidate,
  });
}

export function useDeleteKnowledgeBase() {
  const invalidate = useKnowledgeBaseInvalidator();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => deleteKnowledgeBase(id),
    onSuccess: invalidate,
  });
}
