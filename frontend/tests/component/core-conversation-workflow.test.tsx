import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceNav } from "@/components/app-shell/WorkspaceNav";
import { AuthProvider } from "@/features/auth/AuthProvider";
import { ConditionalConversationSidebar } from "@/features/conversations/ConditionalConversationSidebar";
import { ConversationSidebar } from "@/features/conversations/ConversationSidebar";
import { ConversationsWorkspace } from "@/features/conversations/ConversationsWorkspace";
import { MessageThread } from "@/features/conversations/MessageThread";
import type { Conversation, KnowledgeBase, Page } from "@/lib/api/types";
import { useUiStore } from "@/stores/ui-store";
import { renderWithProviders } from "../helpers";

/** T144 [US4]：会话恢复、错误恢复及输入约束的组件级红灯验收。 */
const TRACE_ID = "trace-conversation-001";

const api = vi.hoisted(() => {
  class ApiError extends Error {
    code: number;
    msg: string;
    status: number;
    traceId: string | null;

    constructor(params: { code: number; msg: string; status: number; traceId: string | null }) {
      super(params.msg);
      this.name = "ApiError";
      this.code = params.code;
      this.msg = params.msg;
      this.status = params.status;
      this.traceId = params.traceId;
    }
  }

  return {
    ApiError,
    asApiError: vi.fn(),
    createConversation: vi.fn(),
    deleteConversation: vi.fn(),
    getConversation: vi.fn(),
    getKnowledgeBase: vi.fn(),
    listCitations: vi.fn(),
    listConversations: vi.fn(),
    listKnowledgeBases: vi.fn(),
    listMessages: vi.fn(),
    renameConversation: vi.fn(),
    streamEvents: vi.fn(),
  };
});

vi.mock("@/lib/api/client", () => api);

const navigation = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
const search = vi.hoisted(() => ({ value: "" }));
const pathname = vi.hoisted(() => ({ value: "/conversations" }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: navigation.replace, push: navigation.push }),
  useSearchParams: () => new URLSearchParams(search.value),
  usePathname: () => pathname.value,
}));

vi.mock("@/features/auth/RequireAuth", () => ({
  RequireAuth: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/app-shell/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/features/citations/CitationDrawer", () => ({ CitationDrawer: () => null }));

const KB_ONE = knowledgeBase("kb-1", "产品研究");
const KB_TWO = knowledgeBase("kb-2", "客户访谈");
const CONVERSATION_ONE = conversation("conversation-1", "kb-1", "已有对话");
const CONVERSATION_TWO = conversation("conversation-2", "kb-2", "恢复的对话");

function knowledgeBase(id: string, name: string): KnowledgeBase {
  return {
    id,
    name,
    description: null,
    status: "active",
    delete_error_code: null,
    allowed_actions: ["delete"],
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:00:00Z",
  };
}

function conversation(
  id: string,
  knowledgeBaseId: string,
  title: string | null,
  knowledgeBaseName = "产品研究"
): Conversation {
  return {
    id,
    knowledge_base_id: knowledgeBaseId,
    knowledge_base_name: knowledgeBaseName,
    title,
    last_message_at: null,
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:00:00Z",
  };
}

function page<T>(items: T[], total = items.length): Page<T> {
  return { items, page: 1, page_size: 20, total };
}

function apiError(message: string, code = 50000, status = 500) {
  return new api.ApiError({ code, msg: message, status, traceId: TRACE_ID });
}

function readyList(items: Conversation[] = [CONVERSATION_ONE]) {
  api.listKnowledgeBases.mockResolvedValue(page([KB_ONE, KB_TWO]));
  api.listConversations.mockResolvedValue(page(items));
}

beforeEach(() => {
  search.value = "";
  pathname.value = "/conversations";
  navigation.replace.mockReset();
  navigation.push.mockReset();
  for (const value of Object.values(api)) {
    if (typeof value === "function" && "mockReset" in value) {
      (value as ReturnType<typeof vi.fn>).mockReset();
    }
  }
  // 错误已由 ApiError 实例承载，收敛函数直接透传。
  api.asApiError.mockImplementation((err: unknown) => err);
  api.listMessages.mockResolvedValue({ items: [], has_more: false, next_before: null });
  api.listCitations.mockResolvedValue(page([]));
  api.streamEvents.mockResolvedValue(undefined);
  useUiStore.setState({
    navCollapsed: false,
    citationDrawerSelector: null,
    documentStatusFilter: "all",
  });
});

afterEach(() => vi.clearAllMocks());

describe("会话恢复与安全上下文", () => {
  it("从 URL 恢复知识库：顶部选择器与会话列表跟随 URL", async () => {
    search.value = "knowledgeBase=kb-2&conversation=conversation-2";
    readyList([CONVERSATION_TWO]);
    api.getConversation.mockResolvedValue(CONVERSATION_TWO);
    api.getKnowledgeBase.mockResolvedValue(KB_TWO);

    renderWithProviders(<ConversationsWorkspace />);

    // 打开选择器验证当前值为 URL 恢复的知识库。
    fireEvent.click(screen.getByRole("button", { name: "选择知识库" }));
    const option = await screen.findByRole("option", { name: "客户访谈" });
    expect(option).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText(/当前对话基于：客户访谈/)).toBeInTheDocument();
  });

  it("对话路由始终展示全局会话历史，不依赖 URL 知识库（T157/T173）", async () => {
    pathname.value = "/conversations";
    search.value = "knowledgeBase=kb-1";
    readyList([CONVERSATION_ONE]);

    renderWithProviders(
      <AuthProvider>
        <WorkspaceNav />
      </AuthProvider>
    );

    expect(await screen.findByText("已有对话")).toBeInTheDocument();
    expect(api.listConversations).toHaveBeenCalledWith(undefined, 1, 20);
  });

  it("非对话路由不展示会话历史（T157）", async () => {
    pathname.value = "/knowledge-bases";
    search.value = "knowledgeBase=kb-1";
    readyList([CONVERSATION_ONE]);

    renderWithProviders(
      <AuthProvider>
        <WorkspaceNav />
      </AuthProvider>
    );

    expect(screen.queryByText("已有对话")).not.toBeInTheDocument();
    expect(api.listConversations).not.toHaveBeenCalled();
  });

  it("对话路由且无明确知识库时仍展示全局会话历史（T157/T173）", async () => {
    pathname.value = "/conversations";
    search.value = "";
    readyList([CONVERSATION_ONE]);

    renderWithProviders(
      <AuthProvider>
        <WorkspaceNav />
      </AuthProvider>
    );

    expect(await screen.findByText("已有对话")).toBeInTheDocument();
    expect(api.listConversations).toHaveBeenCalledWith(undefined, 1, 20);
  });

  it("切换知识库后全局会话历史保持加载，页码不被知识库重置（T173）", async () => {
    pathname.value = "/conversations";
    search.value = "knowledgeBase=kb-1";
    api.listKnowledgeBases.mockResolvedValue(page([KB_ONE, KB_TWO]));
    api.listConversations.mockResolvedValue({
      items: [CONVERSATION_ONE, CONVERSATION_TWO],
      page: 1,
      page_size: 20,
      total: 21,
    });

    const { rerender } = renderWithProviders(<ConditionalConversationSidebar />);
    await screen.findByText("已有对话");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(api.listConversations).toHaveBeenLastCalledWith(undefined, 2, 20));

    // 切换知识库只影响新建范围与正文上下文，侧栏全局历史页码保持不变。
    search.value = "knowledgeBase=kb-2";
    rerender(<ConditionalConversationSidebar />);
    expect(await screen.findByText("已有对话")).toBeInTheDocument();
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
    expect(api.listConversations).not.toHaveBeenLastCalledWith(undefined, 1, 20);
  });

  it("侧栏全局历史不依赖 URL 知识库参数（T173）", async () => {
    search.value = "knowledgeBase=kb-2&conversation=conversation-2";
    readyList([CONVERSATION_TWO]);

    renderWithProviders(<ConversationSidebar />);

    expect(api.listConversations).toHaveBeenCalledWith(undefined, 1, 20);
    expect(await screen.findByText("恢复的对话")).toBeInTheDocument();
  });

  it("知识库超过单页时选择控件仍能访问全部", async () => {
    // 选择器打开时按需加载全部有效知识库（page_size=100，FR-013）。
    const all = Array.from({ length: 21 }, (_, index) =>
      knowledgeBase(`kb-${index + 1}`, `知识库 ${index + 1}`)
    );
    api.listKnowledgeBases.mockResolvedValue(page(all, 21));

    renderWithProviders(<ConversationsWorkspace />);

    fireEvent.click(screen.getByRole("button", { name: "选择知识库" }));
    expect(await screen.findByRole("option", { name: "知识库 21" })).toBeInTheDocument();
    expect(api.listKnowledgeBases).toHaveBeenCalledWith(1, 100);
  });

  it("无效会话显示安全错误、trace_id 与返回会话列表入口", async () => {
    search.value = "knowledgeBase=kb-1&conversation=missing";
    readyList();
    api.getConversation.mockRejectedValue(apiError("不存在的会话", 20007, 404));
    api.getKnowledgeBase.mockResolvedValue(KB_ONE);

    renderWithProviders(<ConversationsWorkspace />);

    const error = await screen.findByRole("alert");
    expect(error).toHaveTextContent("当前内容不存在或已无权访问。");
    expect(error).toHaveTextContent(`trace_id: ${TRACE_ID}`);
    fireEvent.click(screen.getByRole("button", { name: "返回对话列表" }));
    expect(navigation.replace).toHaveBeenCalledWith("/conversations?knowledgeBase=kb-1");
  });

  it("切换会话时关闭属于旧会话的引用抽屉选择", async () => {
    search.value = "knowledgeBase=kb-1&conversation=conversation-1";
    readyList([CONVERSATION_ONE, CONVERSATION_TWO]);
    useUiStore.getState().openCitationDrawer("message-old:1");

    renderWithProviders(<ConversationSidebar />);
    fireEvent.click(await screen.findByRole("button", { name: "打开对话 恢复的对话" }));

    expect(useUiStore.getState().citationDrawerSelector).toBeNull();
    expect(navigation.push).toHaveBeenCalledWith(
      "/conversations?knowledgeBase=kb-2&conversation=conversation-2"
    );
  });

  it("切换知识库时若当前打开着会话，需确认后清空会话上下文", async () => {
    search.value = "knowledgeBase=kb-1&conversation=conversation-1";
    readyList([CONVERSATION_ONE, CONVERSATION_TWO]);
    api.getConversation.mockResolvedValue(CONVERSATION_ONE);
    api.getKnowledgeBase.mockResolvedValue(KB_ONE);

    renderWithProviders(<ConversationsWorkspace />);
    const selectKnowledgeBase = async () => {
      fireEvent.click(screen.getByRole("button", { name: "选择知识库" }));
      fireEvent.click(await screen.findByRole("option", { name: "客户访谈" }));
    };
    await selectKnowledgeBase();

    // 确认弹窗出现；取消则不切换。
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("切换知识库");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(navigation.replace).not.toHaveBeenCalled();

    // 再次切换并确认：URL 清空 conversation 参数并切到新知识库。
    await selectKnowledgeBase();
    fireEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "继续切换" })
    );
    expect(navigation.replace).toHaveBeenCalledWith("/conversations?knowledgeBase=kb-2");
  });
});

describe("阶段 13 体验一致性（T153）", () => {
  it("无 URL 知识库时对话工作区显示选择态，绝不回退到第一个有效知识库", async () => {
    search.value = "";
    readyList([CONVERSATION_ONE]);

    renderWithProviders(<ConversationsWorkspace />);

    // 打开选择器验证：即使存在有效知识库，也没有预选值。
    fireEvent.click(screen.getByRole("button", { name: "选择知识库" }));
    const firstOption = await screen.findByRole("option", { name: "产品研究" });
    expect(firstOption).toHaveAttribute("aria-selected", "false");
    // 未明确选择知识库：主内容区显示选择态（会话历史由全局侧栏按条件呈现）。
    expect(await screen.findByText("选择知识库后，即可输入内容开始新对话。")).toBeInTheDocument();
  });

  it("切换知识库时重置会话页码、选中会话与引用选择", async () => {
    search.value = "knowledgeBase=kb-1&conversation=conversation-1";
    readyList([CONVERSATION_ONE]);
    api.getConversation.mockResolvedValue(CONVERSATION_ONE);
    api.getKnowledgeBase.mockResolvedValue(KB_ONE);
    useUiStore.getState().openCitationDrawer("message-old:1");

    renderWithProviders(<ConversationsWorkspace />);
    fireEvent.click(screen.getByRole("button", { name: "选择知识库" }));
    fireEvent.click(await screen.findByRole("option", { name: "客户访谈" }));
    fireEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "继续切换" })
    );

    // URL 清空 conversation（选中会话重置），引用选择器清空。
    expect(navigation.replace).toHaveBeenCalledWith("/conversations?knowledgeBase=kb-2");
    expect(useUiStore.getState().citationDrawerSelector).toBeNull();
  });

  it("知识库选择器按需加载可访问全部有效知识库", async () => {
    // 超过单页（20）的知识库仍可选择：第二页按需加载。
    const firstPage = Array.from({ length: 20 }, (_, index) =>
      knowledgeBase(`kb-${index + 1}`, `知识库 ${index + 1}`)
    );
    api.listKnowledgeBases
      .mockResolvedValueOnce(page(firstPage, 21))
      .mockResolvedValueOnce(page([knowledgeBase("kb-21", "知识库 21")], 21));

    renderWithProviders(<ConversationsWorkspace />);

    // 展开选择面板并触发按需加载，直到知识库 21 可选。
    fireEvent.click(await screen.findByRole("button", { name: "选择知识库" }));
    expect(await screen.findByRole("option", { name: "知识库 21" })).toBeInTheDocument();
  });

  it("会话删除只发送一次请求", async () => {
    search.value = "knowledgeBase=kb-1&conversation=conversation-1";
    readyList([CONVERSATION_ONE]);
    api.deleteConversation.mockResolvedValue(undefined);

    renderWithProviders(<ConversationSidebar />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "会话操作 已有对话" }));
    await user.click(screen.getByRole("menuitem", { name: "删除" }));
    const confirm = await screen.findByRole("button", { name: "确认删除" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledTimes(1));
  });
});

describe("会话 mutation 的可恢复错误", () => {
  it("空态输入创建失败显示服务端 msg、trace_id 并可重试", async () => {
    search.value = "knowledgeBase=kb-1";
    readyList([]);
    api.createConversation
      .mockRejectedValueOnce(apiError("创建对话失败"))
      .mockResolvedValue(CONVERSATION_ONE);
    renderWithProviders(<ConversationsWorkspace />);

    const starter = screen.getByRole("textbox", { name: "输入问题" });
    fireEvent.change(starter, { target: { value: "第一个问题" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("创建对话失败");
    expect(screen.getByRole("alert")).toHaveTextContent(`trace_id: ${TRACE_ID}`);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(api.createConversation).toHaveBeenCalledTimes(2));
  });

  it("改名失败显示服务端 msg、trace_id 并可重试", async () => {
    search.value = "knowledgeBase=kb-1";
    readyList();
    api.renameConversation
      .mockRejectedValueOnce(apiError("重命名失败"))
      .mockResolvedValue(CONVERSATION_ONE);
    renderWithProviders(<ConversationSidebar />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "会话操作 已有对话" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    fireEvent.change(screen.getByRole("textbox", { name: "新标题" }), {
      target: { value: "新标题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存标题" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("重命名失败");
    expect(screen.getByRole("alert")).toHaveTextContent(`trace_id: ${TRACE_ID}`);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(api.renameConversation).toHaveBeenCalledTimes(2));
  });

  it("删除失败显示服务端 msg、trace_id 并可重试", async () => {
    search.value = "knowledgeBase=kb-1";
    readyList();
    api.deleteConversation
      .mockRejectedValueOnce(apiError("删除对话失败"))
      .mockResolvedValue(undefined);
    renderWithProviders(<ConversationSidebar />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "会话操作 已有对话" }));
    await user.click(screen.getByRole("menuitem", { name: "删除" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认删除" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("删除对话失败");
    expect(screen.getByRole("alert")).toHaveTextContent(`trace_id: ${TRACE_ID}`);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledTimes(2));
  });

  it("空态输入即新建会话：创建成功后自动发送首条内容", async () => {
    search.value = "knowledgeBase=kb-1";
    readyList([]);
    api.createConversation.mockResolvedValue(CONVERSATION_ONE);
    api.getConversation.mockResolvedValue(CONVERSATION_ONE);
    api.getKnowledgeBase.mockResolvedValue(KB_ONE);
    // 模拟真实路由：replace 后 URL 参数变化。
    navigation.replace.mockImplementation((url: string) => {
      search.value = url.split("?")[1] ?? "";
    });
    const { rerender } = renderWithProviders(<ConversationsWorkspace />);

    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "迁移成本有多高？" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() =>
      expect(api.createConversation).toHaveBeenCalledWith({ knowledge_base_id: "kb-1" })
    );
    await waitFor(() =>
      expect(navigation.replace).toHaveBeenCalledWith(
        "/conversations?knowledgeBase=kb-1&conversation=conversation-1"
      )
    );
    // 按新 URL 重渲染后 MessageThread 挂载并自动发送首条内容。
    rerender(<ConversationsWorkspace />);
    await waitFor(() =>
      expect(api.streamEvents).toHaveBeenCalledWith(
        "/conversations/conversation-1/messages",
        expect.objectContaining({ body: { content: "迁移成本有多高？" } })
      )
    );
  });

  it("自动发送首条内容被拒时恢复输入，避免提问丢失", async () => {
    search.value = "knowledgeBase=kb-1";
    readyList([]);
    api.createConversation.mockResolvedValue(CONVERSATION_ONE);
    api.getConversation.mockResolvedValue(CONVERSATION_ONE);
    api.getKnowledgeBase.mockResolvedValue(KB_ONE);
    api.listMessages.mockResolvedValue({ items: [], has_more: false, next_before: null });
    api.streamEvents.mockRejectedValue(apiError("知识库资料尚未处理完成", 20005, 409));
    navigation.replace.mockImplementation((url: string) => {
      search.value = url.split("?")[1] ?? "";
    });
    const { rerender } = renderWithProviders(<ConversationsWorkspace />);

    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "资料准备好了吗？" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() =>
      expect(api.createConversation).toHaveBeenCalledWith({ knowledge_base_id: "kb-1" })
    );
    // 按新 URL 重渲染后 MessageThread 挂载并自动发送首条内容；创建消息前被服务端拒绝。
    rerender(<ConversationsWorkspace />);
    await waitFor(() =>
      expect(api.streamEvents).toHaveBeenCalledWith(
        "/conversations/conversation-1/messages",
        expect.objectContaining({ body: { content: "资料准备好了吗？" } })
      )
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("知识库资料尚未处理完成");
    // 提问回到输入框而非丢失，修正条件后可重试。
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "输入问题" })).toHaveValue("资料准备好了吗？")
    );
  });

  it("新建会话后立即刷新当前知识库的侧栏历史", async () => {
    search.value = "knowledgeBase=kb-1";
    api.listKnowledgeBases.mockResolvedValue(page([KB_ONE, KB_TWO]));
    api.listConversations
      .mockResolvedValueOnce(page([]))
      .mockResolvedValueOnce(page([CONVERSATION_ONE]));
    api.createConversation.mockResolvedValue(CONVERSATION_ONE);
    api.getKnowledgeBase.mockResolvedValue(KB_ONE);

    renderWithProviders(
      <>
        <ConversationSidebar />
        <ConversationsWorkspace />
      </>
    );

    await screen.findByText("尚无对话");
    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "创建后应出现" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByText("已有对话")).toBeInTheDocument();
    expect(api.listConversations).toHaveBeenCalledTimes(2);
  });
});

describe("阶段 13 消息即时体验（T155/T159）", () => {
  it("服务器在创建消息前拒绝时，撤销临时消息并恢复输入", async () => {
    api.listMessages.mockResolvedValue({ items: [], has_more: false, next_before: null });
    api.streamEvents.mockRejectedValue(apiError("知识库资料尚未处理完成", 20005, 409));
    renderWithProviders(<MessageThread conversationId="conversation-1" />);

    const input = screen.getByRole("textbox", { name: "输入问题" });
    fireEvent.change(input, { target: { value: "资料准备好了吗？" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("知识库资料尚未处理完成");
    await waitFor(() =>
      expect(screen.queryByRole("article", { name: "你" })).not.toBeInTheDocument()
    );
    expect(input).toHaveValue("资料准备好了吗？");
  });

  it("用户消息为右侧紧凑气泡、助手为左侧无边框正文（T159）", async () => {
    api.listMessages.mockResolvedValue({
      items: [
        {
          id: "m-user",
          conversation_id: "conversation-1",
          role: "user",
          content: "我的问题",
          status: "completed",
          rewritten_query: null,
          finish_reason: null,
          created_at: "2026-08-20T00:00:00Z",
        },
        {
          id: "m-assistant",
          conversation_id: "conversation-1",
          role: "assistant",
          content: "回答内容",
          status: "completed",
          rewritten_query: null,
          finish_reason: "stop",
          created_at: "2026-08-20T00:00:01Z",
        },
      ],
      has_more: false,
      next_before: null,
    });
    api.listCitations.mockResolvedValue(page([]));

    renderWithProviders(<MessageThread conversationId="conversation-1" />);

    const userArticle = await screen.findByRole("article", { name: "你" });
    expect(userArticle).toHaveClass("justify-end");
    expect(within(userArticle).getByText("我的问题")).toHaveClass("bg-primary");
    const assistantArticle = screen.getByRole("article", { name: "Oriona" });
    expect(assistantArticle).toHaveClass("justify-start");
    expect(assistantArticle.querySelector("div.rounded-2xl")).toBeNull();
  });

  it("流式回答时靛蓝光标附着于助手正文末尾（T159）", async () => {
    api.listMessages.mockResolvedValue({ items: [], has_more: false, next_before: null });
    // 模拟进行中的流：先收 message_start 与 delta，流不结束。
    api.streamEvents.mockImplementation(
      async (_url: string, opts: { onEvent?: (event: string, envelope: unknown) => void }) => {
        opts.onEvent?.("message_start", {
          code: 0,
          msg: "ok",
          trace_id: "trace-stream",
          data: { message_id: "m-stream" },
        });
        opts.onEvent?.("delta", {
          code: 0,
          msg: "ok",
          trace_id: "trace-stream",
          data: { text: "正在生成" },
        });
        return new Promise(() => undefined);
      }
    );

    renderWithProviders(<MessageThread conversationId="conversation-1" />);
    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "问题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    const article = await screen.findByRole("article", { name: "Oriona" });
    await waitFor(() => expect(article.querySelector(".animate-pulse")).not.toBeNull());
    expect(article.textContent).toContain("正在生成");
  });
  it("会话详情加载中显示骨架而不是空白", async () => {
    search.value = "knowledgeBase=kb-1&conversation=conversation-1";
    readyList([CONVERSATION_ONE]);
    api.getConversation.mockReturnValue(new Promise(() => undefined));

    renderWithProviders(<ConversationsWorkspace />);

    expect(await screen.findByLabelText("加载中")).toBeInTheDocument();
  });

  it("发送后立即显示用户消息，不必等终态回写", async () => {
    api.listMessages.mockResolvedValue({ items: [], has_more: false, next_before: null });
    api.streamEvents.mockReturnValue(new Promise(() => undefined));
    renderWithProviders(<MessageThread conversationId="conversation-1" />);

    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "即时显示的问题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByText("即时显示的问题")).toBeInTheDocument();
    expect(screen.getByRole("article", { name: /你/ })).toBeInTheDocument();
  });

  it("消息内容按原有换行渲染", async () => {
    api.listMessages.mockResolvedValue({
      items: [
        {
          id: "m-1",
          conversation_id: "conversation-1",
          role: "assistant",
          content: "第一行\n第二行",
          status: "completed",
          rewritten_query: null,
          finish_reason: "stop",
          created_at: "2026-08-20T00:00:00Z",
        },
      ],
      has_more: false,
      next_before: null,
    });
    api.listCitations.mockResolvedValue({ items: [], page: 1, page_size: 20, total: 0 });

    renderWithProviders(<MessageThread conversationId="conversation-1" />);

    const text = await screen.findByText(/第一行/);
    // 换行原样保留（textContent 含 \n，而非被折叠为空格）。
    expect(text.textContent).toContain("第一行\n第二行");
  });

  it("历史加载失败提供重试入口", async () => {
    api.listMessages
      .mockRejectedValueOnce({ msg: "历史加载失败", traceId: "trace-history" })
      .mockResolvedValue({ items: [], has_more: false, next_before: null });

    renderWithProviders(<MessageThread conversationId="conversation-1" />);

    const error = await screen.findByRole("alert");
    expect(error).toHaveTextContent("历史加载失败");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(api.listMessages).toHaveBeenCalledTimes(2));
  });

  it("用户上翻阅读历史时显示回到最新内容入口", async () => {
    api.listMessages.mockResolvedValue({ items: [], has_more: false, next_before: null });
    renderWithProviders(<MessageThread conversationId="conversation-1" />);
    const scroller = await screen.findByLabelText("消息历史");

    // 模拟用户滚动离开底部（scrollTop 可写，供“回到最新”赋值）。
    Object.defineProperty(scroller, "scrollHeight", {
      configurable: true,
      writable: true,
      value: 2000,
    });
    Object.defineProperty(scroller, "clientHeight", {
      configurable: true,
      writable: true,
      value: 400,
    });
    Object.defineProperty(scroller, "scrollTop", { configurable: true, writable: true, value: 0 });
    fireEvent.scroll(scroller);
    expect(await screen.findByRole("button", { name: "回到最新内容" })).toBeInTheDocument();

    // 回到最新后入口消失，且滚动位置回到底部。
    fireEvent.click(screen.getByRole("button", { name: "回到最新内容" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "回到最新内容" })).not.toBeInTheDocument()
    );
    expect(scroller.scrollTop).toBe(2000);
  });

  it("会话重命名成功显示短暂成功反馈", async () => {
    search.value = "knowledgeBase=kb-1";
    readyList();
    api.renameConversation.mockResolvedValue(CONVERSATION_ONE);
    renderWithProviders(<ConversationSidebar />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "会话操作 已有对话" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    fireEvent.change(screen.getByRole("textbox", { name: "新标题" }), {
      target: { value: "新标题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存标题" }));

    expect(await screen.findByRole("status")).toHaveTextContent(/已保存|成功/);
  });
});

describe("消息加载与输入", () => {
  it("初次加载消息时显示明确的加载状态", () => {
    api.listMessages.mockReturnValue(new Promise(() => undefined));
    renderWithProviders(<MessageThread conversationId="conversation-1" />);
    expect(screen.getByLabelText("正在加载消息")).toBeInTheDocument();
  });

  it("限制输入为 12,000 字并保留 Enter 换行", async () => {
    renderWithProviders(<MessageThread conversationId="conversation-1" />);
    const input = screen.getByRole("textbox", { name: "输入问题" });
    fireEvent.change(input, { target: { value: "a".repeat(12_001) } });
    expect(input).toHaveValue("a".repeat(12_000));
    // 已到上限时 Enter 不再追加换行（不绕过 12,000 字截断）。
    fireEvent.keyDown(input, { key: "Enter" });
    expect(input).toHaveValue("a".repeat(12_000));
    // 未达上限时 Enter 插入换行。
    fireEvent.change(input, { target: { value: "abc" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(input).toHaveValue("abc\n");
  });

  it("Ctrl/Cmd+Enter 发送输入内容", async () => {
    renderWithProviders(<MessageThread conversationId="conversation-1" />);
    const input = screen.getByRole("textbox", { name: "输入问题" });
    fireEvent.change(input, { target: { value: "发送这个问题" } });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });

    await waitFor(() =>
      expect(api.streamEvents).toHaveBeenCalledWith(
        "/conversations/conversation-1/messages",
        expect.objectContaining({ body: { content: "发送这个问题" } })
      )
    );
  });
});

describe("阶段 16 全局会话历史（T171）", () => {
  it("未选择知识库时仍直接分页展示全局历史", async () => {
    search.value = "";
    api.listConversations.mockResolvedValue(page([CONVERSATION_ONE, CONVERSATION_TWO], 2));
    renderWithProviders(<ConversationSidebar />);

    expect(await screen.findByText("已有对话")).toBeInTheDocument();
    expect(screen.getByText("恢复的对话")).toBeInTheDocument();
    expect(api.listConversations).toHaveBeenCalledWith(undefined, 1, 20);
    expect(screen.queryByText("请先选择知识库")).not.toBeInTheDocument();
  });

  it("会话行显示所属知识库名称标签", async () => {
    search.value = "";
    const named = conversation("conversation-3", "kb-2", null, "客户访谈");
    api.listConversations.mockResolvedValue(page([named], 1));
    renderWithProviders(<ConversationSidebar />);

    expect(await screen.findByText("客户访谈")).toBeInTheDocument();
    // 空标题显示固定默认文案“未命名对话”。
    expect(screen.getByText("未命名对话")).toBeInTheDocument();
  });

  it("点击其他知识库的历史会话直接恢复绑定 URL 上下文", async () => {
    search.value = "";
    api.listConversations.mockResolvedValue(page([CONVERSATION_TWO], 1));
    renderWithProviders(<ConversationSidebar />);

    fireEvent.click(await screen.findByRole("button", { name: "打开对话 恢复的对话" }));

    expect(navigation.push).toHaveBeenCalledWith(
      "/conversations?knowledgeBase=kb-2&conversation=conversation-2"
    );
  });

  it("全局历史支持跨知识库分页", async () => {
    search.value = "";
    api.listConversations.mockResolvedValueOnce(page([CONVERSATION_ONE, CONVERSATION_TWO], 21));
    api.listConversations.mockResolvedValueOnce(page([CONVERSATION_ONE], 21));
    renderWithProviders(<ConversationSidebar />);

    fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
    expect(await screen.findByText("2 / 2")).toBeInTheDocument();
    expect(api.listConversations).toHaveBeenLastCalledWith(undefined, 2, 20);
  });

  it("删除当前会话后回到无知识库的全局列表", async () => {
    search.value = "conversation=conversation-1";
    api.listConversations.mockResolvedValue(page([CONVERSATION_ONE], 1));
    api.deleteConversation.mockResolvedValue(undefined);
    renderWithProviders(<ConversationSidebar />);

    await screen.findByRole("button", { name: "会话操作 已有对话" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "会话操作 已有对话" }));
    await user.click(screen.getByRole("menuitem", { name: "删除" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledWith("conversation-1"));
    expect(navigation.replace).toHaveBeenCalledWith("/conversations");
  });

  it("重命名后精确刷新全局历史", async () => {
    search.value = "";
    api.listConversations
      .mockResolvedValueOnce(page([CONVERSATION_ONE], 1))
      .mockResolvedValueOnce(page([conversation("conversation-1", "kb-1", "已重命名")], 1));
    api.renameConversation.mockResolvedValue(conversation("conversation-1", "kb-1", "已重命名"));
    renderWithProviders(<ConversationSidebar />);

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "会话操作 已有对话" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    fireEvent.change(screen.getByRole("textbox", { name: "新标题" }), {
      target: { value: "已重命名" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存标题" }));

    await waitFor(() => expect(api.renameConversation).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("已重命名")).toBeInTheDocument();
    // 全局列表被精确失效并重新拉取。
    await waitFor(() => expect(api.listConversations).toHaveBeenCalledTimes(2));
  });
});
