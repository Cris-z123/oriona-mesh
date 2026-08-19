import { create } from "zustand";

import type { DocumentStatusFilter } from "@/lib/api/types";

/**
 * UI store（T137，ui-design §6.1）：只保存短生命周期客户端 UI 状态——
 * 导航折叠、当前已打开的引用抽屉及其引用 ID、非敏感视图偏好。
 * 不得保存 token、密码、授权结论、资料/会话实体、任务状态或服务端错误码；
 * 服务器状态只由 TanStack Query 管理。
 */

export type { DocumentStatusFilter } from "@/lib/api/types";

interface UiState {
  /** 左侧工作区导航折叠状态。 */
  navCollapsed: boolean;
  /** 当前已打开的引用抽屉引用 ID；null 表示关闭（引用内容只来自 Query 缓存）。 */
  citationDrawerId: string | null;
  /** 非敏感视图偏好：资料列表状态过滤。 */
  documentStatusFilter: DocumentStatusFilter;
  toggleNavCollapsed: () => void;
  openCitationDrawer: (citationId: string) => void;
  closeCitationDrawer: () => void;
  setDocumentStatusFilter: (filter: DocumentStatusFilter) => void;
}

export const useUiStore = create<UiState>()((set) => ({
  navCollapsed: false,
  citationDrawerId: null,
  documentStatusFilter: "all",
  toggleNavCollapsed: () => set((state) => ({ navCollapsed: !state.navCollapsed })),
  openCitationDrawer: (citationId) => set({ citationDrawerId: citationId }),
  closeCitationDrawer: () => set({ citationDrawerId: null }),
  setDocumentStatusFilter: (filter) => set({ documentStatusFilter: filter }),
}));
