import { beforeEach, describe, expect, it } from "vitest";

import { useUiStore } from "@/stores/ui-store";

/**
 * T135 [P] Zustand UI store 单元测试（先写后验）。
 *
 * 职责边界（ui-design §6.1）：只保存短生命周期客户端 UI 状态——
 * 导航折叠、引用抽屉选择器、非敏感视图偏好；
 * 不得保存 token、密码、授权结论、资料/会话实体、任务状态或服务端错误码。
 */

const INITIAL = {
  navCollapsed: false,
  citationDrawerSelector: null,
  documentStatusFilter: "all" as const,
};

beforeEach(() => {
  useUiStore.setState(INITIAL);
});

describe("ui-store：仅保存短生命周期客户端 UI 状态", () => {
  it("初始状态：导航未折叠、引用抽屉关闭、过滤为全部", () => {
    const state = useUiStore.getState();
    expect(state.navCollapsed).toBe(false);
    expect(state.citationDrawerSelector).toBeNull();
    expect(state.documentStatusFilter).toBe("all");
  });

  it("导航折叠状态可切换并恢复", () => {
    useUiStore.getState().toggleNavCollapsed();
    expect(useUiStore.getState().navCollapsed).toBe(true);
    useUiStore.getState().toggleNavCollapsed();
    expect(useUiStore.getState().navCollapsed).toBe(false);
  });

  it("引用抽屉只保存引用选择器，可打开与关闭", () => {
    useUiStore.getState().openCitationDrawer("citation-1");
    expect(useUiStore.getState().citationDrawerSelector).toBe("citation-1");
    useUiStore.getState().closeCitationDrawer();
    expect(useUiStore.getState().citationDrawerSelector).toBeNull();
    // 切换引用只替换 ID，不保存引用内容快照
    useUiStore.getState().openCitationDrawer("citation-2");
    expect(useUiStore.getState().citationDrawerSelector).toBe("citation-2");
  });

  it("视图偏好可保存非敏感资料状态过滤", () => {
    useUiStore.getState().setDocumentStatusFilter("failed");
    expect(useUiStore.getState().documentStatusFilter).toBe("failed");
    useUiStore.getState().setDocumentStatusFilter("all");
    expect(useUiStore.getState().documentStatusFilter).toBe("all");
  });

  it("公开状态形状固定：不保存服务端实体、令牌、授权结论或错误码", () => {
    const state = useUiStore.getState();
    const forbidden = [
      "accessToken",
      "refreshToken",
      "expiresAt",
      "user",
      "documents",
      "knowledgeBases",
      "conversations",
      "messages",
      "citations",
      "errorCode",
      "allowedActions",
      "status",
      "traceId",
    ];
    for (const key of forbidden) {
      expect(state).not.toHaveProperty(key);
    }
    // 结构不变量：仅 UI 字段与动作，无任何服务端状态槽位
    expect(Object.keys(state).sort()).toEqual([
      "citationDrawerSelector",
      "closeCitationDrawer",
      "documentStatusFilter",
      "navCollapsed",
      "openCitationDrawer",
      "setDocumentStatusFilter",
      "toggleNavCollapsed",
    ]);
  });
});
