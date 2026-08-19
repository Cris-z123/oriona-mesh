import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CitationCard } from "@/features/citations/CitationCard";
import { CitationDrawer } from "@/features/citations/CitationDrawer";
import { ConversationFeedback } from "@/features/conversations/ConversationFeedback";
import { ConversationList } from "@/features/conversations/ConversationList";
import { MessageThread } from "@/features/conversations/MessageThread";
import { useUiStore } from "@/stores/ui-store";
import { renderWithProviders } from "../helpers";

const api = vi.hoisted(() => ({
  listKnowledgeBases: vi.fn(),
  listConversations: vi.fn(),
  createConversation: vi.fn(),
  renameConversation: vi.fn(),
  deleteConversation: vi.fn(),
  listMessages: vi.fn(),
  listCitations: vi.fn(),
  streamEvents: vi.fn(),
}));

vi.mock("@/lib/api/client", () => api);

const knowledgeBase = {
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  name: "产品研究",
  description: null,
  status: "active" as const,
  delete_error_code: null,
  allowed_actions: ["delete" as const],
  created_at: "2026-08-19T00:00:00Z",
  updated_at: "2026-08-19T00:00:00Z",
};

const conversation = {
  id: "11111111-1111-4111-8111-111111111111",
  knowledge_base_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  title: null,
  last_message_at: null,
  created_at: "2026-08-19T00:00:00Z",
  updated_at: "2026-08-19T00:00:00Z",
};

const userMessage = {
  id: "22222222-2222-4222-8222-222222222222",
  conversation_id: "11111111-1111-4111-8111-111111111111",
  role: "user" as const,
  content: "北美市场有哪些阻力？",
  status: "completed" as const,
  rewritten_query: "北美市场阻力",
  finish_reason: null,
  created_at: "2026-08-19T00:00:00Z",
};

const assistantMessage = {
  id: "33333333-3333-4333-8333-333333333333",
  conversation_id: "11111111-1111-4111-8111-111111111111",
  role: "assistant" as const,
  content: "迁移成本是首要阻力。",
  status: "completed" as const,
  rewritten_query: null,
  finish_reason: "stop" as const,
  created_at: "2026-08-19T00:01:00Z",
};

const liveCitation = {
  rank: 2,
  score: 0.84,
  chunk_id: "44444444-4444-4444-8444-444444444444",
  document_id: "55555555-5555-4555-8555-555555555555",
  document_version: 1,
  filename: "客户访谈纪要.pdf",
  file_type: "pdf" as const,
  page: 3,
  section: "迁移成本",
  content: "迁移既有流程的成本尚未被说清。",
  source_type: "live" as const,
};

const snapshotCitation = {
  ...liveCitation,
  rank: 1,
  chunk_id: null,
  document_id: null,
  filename: "已删除资料快照",
  source_type: "snapshot" as const,
};

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  useUiStore.setState({
    navCollapsed: false,
    citationDrawerSelector: null,
    documentStatusFilter: "all",
  });
});

describe("US2 会话必须绑定知识库", () => {
  it("仅允许选择当前可用知识库后创建对话，并以未命名标题展示空标题", async () => {
    api.listKnowledgeBases.mockResolvedValue({
      items: [knowledgeBase],
      page: 1,
      page_size: 20,
      total: 1,
    });
    api.listConversations.mockResolvedValue({
      items: [conversation],
      page: 1,
      page_size: 20,
      total: 1,
    });
    api.createConversation.mockResolvedValue(conversation);

    renderWithProviders(<ConversationList />);
    const create = screen.getByRole("button", { name: "新建对话" });
    expect(create).toBeDisabled();
    await screen.findByRole("option", { name: "产品研究" });
    fireEvent.change(screen.getByRole("combobox", { name: "选择知识库" }), {
      target: { value: knowledgeBase.id },
    });
    await waitFor(() => expect(create).toBeEnabled());
    expect(await screen.findByText("未命名对话")).toBeInTheDocument();
    fireEvent.click(create);

    await waitFor(() =>
      expect(api.createConversation).toHaveBeenCalledWith({ knowledge_base_id: knowledgeBase.id })
    );
  });

  it("按页加载会话，并保留当前知识库范围", async () => {
    api.listKnowledgeBases.mockResolvedValue({
      items: [knowledgeBase],
      page: 1,
      page_size: 20,
      total: 1,
    });
    api.listConversations
      .mockResolvedValueOnce({ items: [conversation], page: 1, page_size: 20, total: 21 })
      .mockResolvedValueOnce({
        items: [{ ...conversation, id: "conversation-2", title: "下一页" }],
        page: 2,
        page_size: 20,
        total: 21,
      });

    renderWithProviders(<ConversationList />);
    await screen.findByRole("option", { name: "产品研究" });
    fireEvent.change(screen.getByRole("combobox", { name: "选择知识库" }), {
      target: { value: knowledgeBase.id },
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "新建对话" })).toBeEnabled());
    await screen.findByText("未命名对话");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(screen.getAllByText("下一页")).toHaveLength(2));
    expect(api.listConversations).toHaveBeenLastCalledWith(knowledgeBase.id, 2, 20);
  });

  it("允许重命名和删除当前知识库内的对话，并刷新列表", async () => {
    const namedConversation = { ...conversation, title: "旧标题" };
    api.listKnowledgeBases.mockResolvedValue({
      items: [knowledgeBase],
      page: 1,
      page_size: 20,
      total: 1,
    });
    api.listConversations.mockResolvedValue({
      items: [namedConversation],
      page: 1,
      page_size: 20,
      total: 1,
    });
    api.renameConversation.mockResolvedValue({ ...namedConversation, title: "新标题" });
    api.deleteConversation.mockResolvedValue(undefined);

    renderWithProviders(<ConversationList />);
    await screen.findByRole("option", { name: "产品研究" });
    fireEvent.change(screen.getByRole("combobox", { name: "选择知识库" }), {
      target: { value: knowledgeBase.id },
    });
    await screen.findByText("旧标题");
    fireEvent.click(screen.getByRole("button", { name: "重命名 旧标题" }));
    fireEvent.change(screen.getByLabelText("新标题"), { target: { value: "新标题" } });
    fireEvent.click(screen.getByRole("button", { name: "保存标题" }));
    await waitFor(() =>
      expect(api.renameConversation).toHaveBeenCalledWith(conversation.id, { title: "新标题" })
    );

    fireEvent.click(screen.getByRole("button", { name: "删除对话 旧标题" }));
    await waitFor(() => expect(api.deleteConversation).toHaveBeenCalledWith(conversation.id));
  });

  it("删除当前查看的会话后通知宿主清空选择", async () => {
    const onSelectConversation = vi.fn();
    api.listKnowledgeBases.mockResolvedValue({
      items: [knowledgeBase],
      page: 1,
      page_size: 20,
      total: 1,
    });
    api.listConversations.mockResolvedValue({
      items: [conversation],
      page: 1,
      page_size: 20,
      total: 1,
    });
    api.deleteConversation.mockResolvedValue(undefined);

    renderWithProviders(
      <ConversationList
        onSelectConversation={onSelectConversation}
        selectedConversationId={conversation.id}
      />
    );
    await screen.findByRole("option", { name: "产品研究" });
    fireEvent.change(screen.getByRole("combobox", { name: "选择知识库" }), {
      target: { value: knowledgeBase.id },
    });
    fireEvent.click(await screen.findByRole("button", { name: "删除对话 未命名对话" }));

    await waitFor(() => expect(onSelectConversation).toHaveBeenCalledWith(null));
  });

  it("会话列表明确呈现加载与可恢复错误状态", async () => {
    api.listKnowledgeBases.mockResolvedValue({
      items: [knowledgeBase],
      page: 1,
      page_size: 20,
      total: 1,
    });
    api.listConversations.mockRejectedValue(new Error("network"));

    renderWithProviders(<ConversationList />);
    await screen.findByRole("option", { name: "产品研究" });
    fireEvent.change(screen.getByRole("combobox", { name: "选择知识库" }), {
      target: { value: knowledgeBase.id },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("无法加载对话");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(api.listConversations).toHaveBeenCalledTimes(2));
  });

  it("选择会话时把会话 ID 交给消息线程宿主", async () => {
    const onSelectConversation = vi.fn();
    api.listKnowledgeBases.mockResolvedValue({
      items: [knowledgeBase],
      page: 1,
      page_size: 20,
      total: 1,
    });
    api.listConversations.mockResolvedValue({
      items: [conversation],
      page: 1,
      page_size: 20,
      total: 1,
    });

    renderWithProviders(<ConversationList onSelectConversation={onSelectConversation} />);
    await screen.findByRole("option", { name: "产品研究" });
    fireEvent.change(screen.getByRole("combobox", { name: "选择知识库" }), {
      target: { value: knowledgeBase.id },
    });
    fireEvent.click(await screen.findByRole("button", { name: "打开对话 未命名对话" }));
    expect(onSelectConversation).toHaveBeenCalledWith(conversation);
  });
});

describe("US2 消息、SSE 与用户反馈", () => {
  it("按游标加载更早的消息历史，并为历史回答加载紧凑引用卡", async () => {
    api.listMessages
      .mockResolvedValueOnce({
        items: [assistantMessage],
        has_more: true,
        next_before: "33333333-3333-4333-8333-333333333333",
      })
      .mockResolvedValueOnce({
        items: [userMessage],
        has_more: false,
        next_before: null,
      });
    api.listCitations.mockResolvedValue({
      items: [liveCitation],
      page: 1,
      page_size: 20,
      total: 1,
    });

    renderWithProviders(<MessageThread conversationId={conversation.id} />);
    expect(await screen.findByText(assistantMessage.content)).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /查看引用.*客户访谈纪要\.pdf/ })
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));

    expect(await screen.findByText(userMessage.content)).toBeInTheDocument();
    expect(api.listMessages).toHaveBeenLastCalledWith(
      conversation.id,
      "33333333-3333-4333-8333-333333333333",
      50
    );
  });

  it("历史回答按需加载后续引用页", async () => {
    const laterCitation = { ...liveCitation, rank: 21, content: "第二页引用" };
    api.listMessages.mockResolvedValue({
      items: [assistantMessage],
      has_more: false,
      next_before: null,
    });
    api.listCitations
      .mockResolvedValueOnce({ items: [liveCitation], page: 1, page_size: 20, total: 21 })
      .mockResolvedValueOnce({ items: [laterCitation], page: 2, page_size: 20, total: 21 });

    renderWithProviders(<MessageThread conversationId={conversation.id} />);
    await screen.findByRole("button", { name: /查看引用.*客户访谈纪要\.pdf/ });
    fireEvent.click(screen.getByRole("button", { name: "加载更多引用" }));

    expect(await screen.findByText("[21] 客户访谈纪要.pdf")).toBeInTheDocument();
    expect(api.listCitations).toHaveBeenNthCalledWith(
      2,
      conversation.id,
      assistantMessage.id,
      2,
      20
    );
  });

  it("解析五类 SSE 增量并在完成后显示回答和证据卡", async () => {
    api.listMessages
      .mockResolvedValueOnce({ items: [userMessage], has_more: false, next_before: null })
      .mockResolvedValueOnce({
        items: [assistantMessage, userMessage],
        has_more: false,
        next_before: null,
      });
    api.listCitations.mockResolvedValue({
      items: [liveCitation],
      page: 1,
      page_size: 20,
      total: 1,
    });
    api.streamEvents.mockImplementation(
      async (_path: string, options: { onEvent: (event: string, data: unknown) => void }) => {
        options.onEvent("message_start", {
          code: 0,
          msg: "ok",
          trace_id: "trace-1",
          data: { message_id: assistantMessage.id },
        });
        options.onEvent("retrieval_done", {
          code: 0,
          msg: "ok",
          trace_id: "trace-1",
          data: { citations: [liveCitation] },
        });
        options.onEvent("delta", {
          code: 0,
          msg: "ok",
          trace_id: "trace-1",
          data: { text: "迁移成本" },
        });
        options.onEvent("message_end", {
          code: 0,
          msg: "ok",
          trace_id: "trace-1",
          data: { message_id: assistantMessage.id, finish_reason: "stop" },
        });
      }
    );

    renderWithProviders(<MessageThread conversationId={conversation.id} />);
    await screen.findByText(userMessage.content);
    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "继续总结" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect((await screen.findAllByText("迁移成本"))[0]).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /查看引用.*客户访谈纪要\.pdf/ })
    ).toBeInTheDocument();
    expect(api.streamEvents).toHaveBeenCalledWith(
      `/conversations/${conversation.id}/messages`,
      expect.objectContaining({ body: { content: "继续总结" } })
    );
  });

  it("把 SSE error 与用户取消保留为不同终态", async () => {
    api.listMessages.mockResolvedValue({ items: [], has_more: false, next_before: null });
    api.streamEvents.mockImplementationOnce(
      async (_path: string, options: { onEvent: (event: string, data: unknown) => void }) => {
        options.onEvent("message_start", {
          code: 0,
          msg: "ok",
          trace_id: "trace-1",
          data: { message_id: assistantMessage.id },
        });
        options.onEvent("error", {
          code: 50000,
          msg: "模型服务暂时不可用",
          trace_id: "trace-error",
          data: {},
        });
      }
    );

    const firstRender = renderWithProviders(<MessageThread conversationId={conversation.id} />);
    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "会失败的问题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("模型服务暂时不可用");
    expect(screen.getByRole("alert")).toHaveTextContent("trace_id: trace-error");

    api.streamEvents.mockImplementationOnce(
      (
        _path: string,
        options: { signal: AbortSignal; onEvent: (event: string, data: unknown) => void }
      ) => {
        options.onEvent("message_start", {
          code: 0,
          msg: "ok",
          trace_id: "trace-2",
          data: { message_id: assistantMessage.id },
        });
        return new Promise<void>((_resolve, reject) => {
          options.signal.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError"))
          );
        });
      }
    );
    firstRender.unmount();
    renderWithProviders(<MessageThread conversationId={conversation.id} />);
    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "取消的问题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByRole("button", { name: "取消" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(await screen.findByText("回答已取消。")).toBeInTheDocument();
  });

  it("分别呈现可信无证据、服务失败和客户端取消终态", () => {
    const { rerender } = renderWithProviders(<ConversationFeedback kind="no_evidence" />);
    expect(screen.getByText(/未找到相关证据/)).toBeInTheDocument();

    rerender(
      <ConversationFeedback kind="failed" message="模型服务暂时不可用" traceId="trace-failed" />
    );
    expect(screen.getByText("模型服务暂时不可用")).toBeInTheDocument();
    expect(screen.getByText("trace_id: trace-failed")).toBeInTheDocument();

    rerender(<ConversationFeedback kind="failed" code={10001} />);
    expect(screen.getByText("登录状态已过期，请重新登录。")).toBeInTheDocument();

    rerender(<ConversationFeedback kind="failed" code={20007} status={404} />);
    expect(screen.getByText("当前内容不存在或已无权访问。")).toBeInTheDocument();

    rerender(<ConversationFeedback kind="failed" code={10005} retryAfter={30} />);
    expect(screen.getByText("请求过于频繁，请于 30 秒后重试。")).toBeInTheDocument();

    rerender(<ConversationFeedback kind="cancelled" />);
    expect(screen.getByText(/回答已取消/)).toBeInTheDocument();
  });
});

describe("US2 消息流生命周期与排序", () => {
  it("在终态重新获取服务端历史后不再保留本地草稿副本", async () => {
    const persistedAssistant = { ...assistantMessage, content: "唯一的持久化回答" };
    api.listMessages
      .mockResolvedValueOnce({ items: [userMessage], has_more: false, next_before: null })
      .mockResolvedValueOnce({
        items: [persistedAssistant, userMessage],
        has_more: false,
        next_before: null,
      });
    api.streamEvents.mockImplementation(
      async (_path: string, options: { onEvent: (event: string, data: unknown) => void }) => {
        options.onEvent("message_start", {
          code: 0,
          msg: "ok",
          trace_id: "trace-1",
          data: { message_id: assistantMessage.id },
        });
        options.onEvent("delta", {
          code: 0,
          msg: "ok",
          trace_id: "trace-1",
          data: { text: "唯一的持久化回答" },
        });
        options.onEvent("message_end", {
          code: 0,
          msg: "ok",
          trace_id: "trace-1",
          data: { message_id: assistantMessage.id, finish_reason: "stop" },
        });
      }
    );

    renderWithProviders(<MessageThread conversationId={conversation.id} />);
    await screen.findByText(userMessage.content);
    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "继续总结" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(screen.getAllByText("唯一的持久化回答")).toHaveLength(1));
  });

  it("把服务端倒序游标页转为时间正序，并把更早页前插", async () => {
    const newerUser = { ...userMessage, content: "较新的问题", created_at: "2026-08-19T00:02:00Z" };
    const newerAssistant = {
      ...assistantMessage,
      content: "较新的回答",
      created_at: "2026-08-19T00:03:00Z",
    };
    const olderUser = {
      ...userMessage,
      id: "66666666-6666-4666-8666-666666666666",
      content: "较早的问题",
      created_at: "2026-08-19T00:00:00Z",
    };
    const olderAssistant = {
      ...assistantMessage,
      id: "77777777-7777-4777-8777-777777777777",
      content: "较早的回答",
      created_at: "2026-08-19T00:01:00Z",
    };
    api.listMessages
      .mockResolvedValueOnce({
        items: [newerAssistant, newerUser],
        has_more: true,
        next_before: newerUser.id,
      })
      .mockResolvedValueOnce({
        items: [olderAssistant, olderUser],
        has_more: false,
        next_before: null,
      });
    api.listCitations.mockResolvedValue({ items: [], page: 1, page_size: 20, total: 0 });

    renderWithProviders(<MessageThread conversationId={conversation.id} />);
    await screen.findByText("较新的回答");
    expect(screen.getAllByRole("article").map((node) => node.textContent)).toEqual([
      expect.stringContaining("较新的问题"),
      expect.stringContaining("较新的回答"),
    ]);
    fireEvent.click(screen.getByRole("button", { name: "加载更早消息" }));
    await screen.findByText("较早的问题");
    expect(screen.getAllByRole("article").map((node) => node.textContent)).toEqual([
      expect.stringContaining("较早的问题"),
      expect.stringContaining("较早的回答"),
      expect.stringContaining("较新的问题"),
      expect.stringContaining("较新的回答"),
    ]);
  });

  it("卸载消息线程时中止仍在进行的 SSE 连接", async () => {
    api.listMessages.mockResolvedValue({ items: [], has_more: false, next_before: null });
    let signal: AbortSignal | undefined;
    api.streamEvents.mockImplementation((_path: string, options: { signal: AbortSignal }) => {
      signal = options.signal;
      return new Promise<void>(() => undefined);
    });

    const view = renderWithProviders(<MessageThread conversationId={conversation.id} />);
    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "仍在生成" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(signal).toBeDefined());
    view.unmount();

    expect(signal?.aborted).toBe(true);
  });

  it("切换会话时中止旧会话仍在进行的 SSE 连接", async () => {
    api.listMessages.mockResolvedValue({ items: [], has_more: false, next_before: null });
    let signal: AbortSignal | undefined;
    api.streamEvents.mockImplementation((_path: string, options: { signal: AbortSignal }) => {
      signal = options.signal;
      return new Promise<void>(() => undefined);
    });

    const view = renderWithProviders(<MessageThread conversationId={conversation.id} />);
    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "仍在生成" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(signal).toBeDefined());
    view.rerender(<MessageThread conversationId="88888888-8888-4888-8888-888888888888" />);

    expect(signal?.aborted).toBe(true);
  });

  it("SSE 在未收到终态事件时收敛为可理解的失败反馈", async () => {
    api.listMessages.mockResolvedValue({ items: [], has_more: false, next_before: null });
    api.streamEvents.mockImplementation(
      async (_path: string, options: { onEvent: (event: string, data: unknown) => void }) => {
        options.onEvent("message_start", {
          code: 0,
          msg: "ok",
          trace_id: "trace-eof",
          data: { message_id: assistantMessage.id },
        });
      }
    );

    renderWithProviders(<MessageThread conversationId={conversation.id} />);
    fireEvent.change(screen.getByRole("textbox", { name: "输入问题" }), {
      target: { value: "意外断流" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("回答连接意外中断，请重试。");
  });
});

describe("US2 引用", () => {
  it("按 rank 呈现紧凑证据卡，且键盘可打开并关闭抽屉", async () => {
    api.listCitations.mockResolvedValue({
      items: [snapshotCitation, liveCitation],
      page: 1,
      page_size: 20,
      total: 2,
    });
    renderWithProviders(
      <>
        <CitationCard
          messageId={assistantMessage.id}
          citations={[liveCitation, snapshotCitation]}
        />
        <CitationDrawer conversationId={conversation.id} />
      </>
    );

    const cards = screen.getAllByRole("button", { name: /查看引用/ });
    expect(cards[0]).toHaveTextContent("已删除资料快照");
    fireEvent.keyDown(cards[0]!, { key: "Enter" });
    fireEvent.click(cards[0]!);
    expect(await screen.findByRole("dialog", { name: "引用详情" })).toBeInTheDocument();
    expect(await screen.findByText(/来源快照/)).toBeInTheDocument();
    expect(api.listCitations).toHaveBeenCalledWith(conversation.id, assistantMessage.id, 1, 20);
    expect(screen.queryByRole("link", { name: /打开原始资料/ })).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("dialog", { name: "引用详情" }), { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "引用详情" })).not.toBeInTheDocument()
    );
  });

  it("live 引用仅在服务端提供当前来源 ID 时提供定位入口", async () => {
    api.listCitations.mockResolvedValue({
      items: [liveCitation],
      page: 1,
      page_size: 20,
      total: 1,
    });
    renderWithProviders(
      <>
        <CitationCard messageId={assistantMessage.id} citations={[liveCitation]} />
        <CitationDrawer conversationId={conversation.id} knowledgeBaseId={knowledgeBase.id} />
      </>
    );
    fireEvent.click(screen.getByRole("button", { name: /查看引用.*客户访谈纪要.pdf/ }));
    expect(await screen.findByRole("link", { name: "定位到资料" })).toHaveAttribute(
      "href",
      `/knowledge-bases/${knowledgeBase.id}?document=${liveCitation.document_id}`
    );
  });

  it("按需继续加载引用页，直到找到首批之外的 rank", async () => {
    const laterCitation = { ...liveCitation, rank: 21, content: "第二页的来源片段" };
    api.listCitations
      .mockResolvedValueOnce({ items: [liveCitation], page: 1, page_size: 20, total: 21 })
      .mockResolvedValueOnce({ items: [laterCitation], page: 2, page_size: 20, total: 21 });
    renderWithProviders(
      <>
        <CitationCard messageId={assistantMessage.id} citations={[laterCitation]} />
        <CitationDrawer conversationId={conversation.id} knowledgeBaseId={knowledgeBase.id} />
      </>
    );

    fireEvent.click(screen.getByRole("button", { name: /查看引用.*客户访谈纪要.pdf/ }));
    expect(await screen.findByText("第二页的来源片段")).toBeInTheDocument();
    expect(api.listCitations).toHaveBeenNthCalledWith(
      1,
      conversation.id,
      assistantMessage.id,
      1,
      20
    );
    expect(api.listCitations).toHaveBeenNthCalledWith(
      2,
      conversation.id,
      assistantMessage.id,
      2,
      20
    );
  });

  it("引用详情请求失败时提供重试入口", async () => {
    api.listCitations.mockRejectedValue(new Error("network"));
    renderWithProviders(
      <>
        <CitationCard messageId={assistantMessage.id} citations={[liveCitation]} />
        <CitationDrawer conversationId={conversation.id} />
      </>
    );

    fireEvent.click(screen.getByRole("button", { name: /查看引用.*客户访谈纪要.pdf/ }));
    expect(await screen.findByText("引用详情加载失败，请检查网络后重试。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(api.listCitations).toHaveBeenCalledTimes(2));
  });
});
