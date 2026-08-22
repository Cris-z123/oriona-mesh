import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../helpers";
import KnowledgeBaseDocumentsPage from "@/app/knowledge-bases/[knowledgeBaseId]/page";
import { DocumentList } from "@/features/documents/DocumentList";
import { UploadPanel } from "@/features/documents/UploadPanel";
import { KnowledgeBaseList } from "@/features/knowledge-bases/KnowledgeBaseList";
import type { Document, KnowledgeBase, Page } from "@/lib/api/types";
import { useUiStore } from "@/stores/ui-store";

/** T143 [US4]：知识库到资料工作区的核心路径组件级红灯验收。 */
const TRACE_ID = "trace-documents-001";

const api = vi.hoisted(() => {
  class ApiError extends Error {
    code: number;
    msg: string;
    status: number;
    traceId: string | null;

    constructor({
      code,
      msg,
      status,
      traceId,
    }: {
      code: number;
      msg: string;
      status: number;
      traceId: string | null;
    }) {
      super(msg);
      this.name = "ApiError";
      this.code = code;
      this.msg = msg;
      this.status = status;
      this.traceId = traceId;
    }
  }

  return {
    ApiError,
    asApiError: (error: unknown) =>
      error instanceof ApiError
        ? error
        : new ApiError({ code: 50000, msg: "请求失败", status: 500, traceId: null }),
    createKnowledgeBase: vi.fn(),
    deleteDocument: vi.fn(),
    deleteKnowledgeBase: vi.fn(),
    generateIdempotencyKey: vi.fn(() => "upload-key-001"),
    getDocument: vi.fn(),
    getKnowledgeBase: vi.fn(),
    listDocumentTasks: vi.fn(),
    listDocuments: vi.fn(),
    listKnowledgeBases: vi.fn(),
    updateKnowledgeBase: vi.fn(),
    uploadDocuments: vi.fn(),
  };
});

vi.mock("@/lib/api/client", () => api);

const navigation = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useParams: () => ({ knowledgeBaseId: "kb-1" }),
  useRouter: () => ({
    push: navigation.push,
    replace: navigation.replace,
    back: vi.fn(),
    forward: vi.fn(),
    refresh: vi.fn(),
    prefetch: vi.fn(),
  }),
  useSearchParams: () => new URLSearchParams(),
}));

// 页面编排在此文件是被测对象；认证和壳层不属于 T143 的资料工作区验收范围。
vi.mock("@/features/auth/RequireAuth", () => ({
  RequireAuth: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/app-shell/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const KNOWLEDGE_BASE = {
  id: "kb-1",
  name: "旅行笔记",
  description: "出发前的整理",
  status: "active",
  delete_error_code: null,
  allowed_actions: ["delete"],
  created_at: "2026-08-20T00:00:00Z",
  updated_at: "2026-08-20T00:00:00Z",
} satisfies KnowledgeBase;

function documentFixture(overrides: Partial<Document> = {}): Document {
  return {
    id: "doc-1",
    knowledge_base_id: "kb-1",
    filename: "行程.txt",
    file_type: "txt",
    file_size: 1024,
    status: "completed",
    version: 1,
    current_task_type: null,
    retry_count: 0,
    delete_cycle: 0,
    chunk_count: 2,
    error_code: null,
    error_message: null,
    processing_started_at: "2026-08-20T00:00:00Z",
    processing_finished_at: "2026-08-20T00:01:00Z",
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:01:00Z",
    allowed_actions: ["delete"],
    ...overrides,
  };
}

function page<T>(items: T[]): Page<T> {
  return { items, page: 1, page_size: 20, total: items.length };
}

function apiError(message: string) {
  return new api.ApiError({ code: 50000, msg: message, status: 500, traceId: TRACE_ID });
}

function selectUpload(files: File[]) {
  fireEvent.change(screen.getByLabelText("选择文件"), { target: { files } });
}

beforeEach(() => {
  navigation.push.mockReset();
  navigation.replace.mockReset();
  for (const value of Object.values(api)) {
    if (typeof value === "function" && "mockReset" in value) {
      (value as ReturnType<typeof vi.fn>).mockReset();
    }
  }
  api.generateIdempotencyKey.mockReturnValue("upload-key-001");
  api.listDocumentTasks.mockResolvedValue(page([]));
  useUiStore.getState().setDocumentStatusFilter("all");
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("知识库进入资料工作区", () => {
  it("创建成功后直接进入新知识库的资料工作区", async () => {
    api.listKnowledgeBases.mockResolvedValue(page([KNOWLEDGE_BASE]));
    api.createKnowledgeBase.mockResolvedValue(KNOWLEDGE_BASE);

    renderWithProviders(<KnowledgeBaseList />);
    await screen.findByText("旅行笔记");
    fireEvent.change(screen.getByLabelText("知识库名称"), { target: { value: "旅行笔记" } });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith("/knowledge-bases/kb-1"));
  });

  it("知识库列表提供打开资料入口，退出后可再次进入资料工作区", async () => {
    api.listKnowledgeBases.mockResolvedValue(page([KNOWLEDGE_BASE]));

    renderWithProviders(<KnowledgeBaseList />);
    await screen.findByText("旅行笔记");

    const entryLink = screen.getByRole("link", { name: "打开资料 旅行笔记" });
    expect(entryLink).toHaveAttribute("href", "/knowledge-bases/kb-1");
    expect(screen.getByRole("button", { name: "打开资料" })).toBeInTheDocument();
  });

  it("编辑时允许将已有描述明确保存为空字符串", async () => {
    api.listKnowledgeBases.mockResolvedValue(page([KNOWLEDGE_BASE]));
    api.updateKnowledgeBase.mockResolvedValue({ ...KNOWLEDGE_BASE, description: "" });

    renderWithProviders(<KnowledgeBaseList />);
    await screen.findByText("出发前的整理");
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("描述", { selector: "#edit-desc-kb-1" }), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(api.updateKnowledgeBase).toHaveBeenCalledWith("kb-1", {
        name: "旅行笔记",
        description: "",
      })
    );
  });
});

describe("资料上传与筛选后终态", () => {
  it("上传被接受后反馈本批资料数量和正在处理状态", async () => {
    api.uploadDocuments.mockResolvedValue({
      documents: [
        documentFixture({ id: "doc-queued", status: "queued", current_task_type: "parse" }),
        documentFixture({ id: "doc-processing", status: "processing", current_task_type: "chunk" }),
      ],
    });

    render(<UploadPanel knowledgeBaseId="kb-1" />);
    selectUpload([
      new File(["a"], "行程.txt", { type: "text/plain" }),
      new File(["b"], "预算.md", { type: "text/markdown" }),
    ]);

    expect(await screen.findByText("已接收 2 份资料，正在处理")).toBeInTheDocument();
  });

  it("当前筛选不显示新上传资料时，仍追踪该批次直到终态并刷新可见列表", async () => {
    const existing = documentFixture({ id: "doc-existing", filename: "已完成.txt" });
    const uploaded = documentFixture({
      id: "doc-new",
      filename: "稍后完成.txt",
      status: "processing",
      current_task_type: "parse",
    });
    const finalDocument = { ...uploaded, status: "completed" as const, current_task_type: null };
    api.uploadDocuments.mockResolvedValue({ documents: [uploaded] });
    // 可见列表（completed 筛选）在批次终态前不含新资料；
    // 批次跟踪查询（不过滤）第一次返回 processing，其后返回 completed。
    let batchTerminal = false;
    api.listDocuments.mockImplementation(async (_kbId, _page, _pageSize, status) => {
      if (status !== undefined) return page(batchTerminal ? [existing, finalDocument] : [existing]);
      const result = batchTerminal ? [existing, finalDocument] : [existing, uploaded];
      batchTerminal = true;
      return page(result);
    });
    useUiStore.getState().setDocumentStatusFilter("completed");

    renderWithProviders(<KnowledgeBaseDocumentsPage />);
    await screen.findByText("已完成.txt");
    vi.useFakeTimers();
    selectUpload([new File(["later"], "稍后完成.txt", { type: "text/plain" })]);

    // 上传后批次查询立即启动：仍在处理中，可见列表（completed 筛选）不变。
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByText("稍后完成.txt")).not.toBeInTheDocument();

    // 首轮轮询（3 秒）拿到批次终态 → 失效子树 → 可见列表重取并展示新资料。
    await act(async () => {
      for (let tick = 0; tick < 6; tick++) {
        await vi.advanceTimersByTimeAsync(1_000);
        for (let i = 0; i < 20; i++) await Promise.resolve();
      }
    });
    expect(screen.getByText("稍后完成.txt")).toBeInTheDocument();

    // 批次已收敛：轮询停止，列表保持终态内容。
    await act(async () => {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(screen.getByText("稍后完成.txt")).toBeInTheDocument();
  });
});

describe("资料详情上下文", () => {
  it("详情以可关闭抽屉打开，删除成功后关闭并清除资料上下文", async () => {
    const doc = documentFixture();
    api.listDocuments.mockResolvedValue(page([doc]));
    api.getDocument.mockResolvedValue(doc);
    api.deleteDocument.mockResolvedValue(undefined);

    renderWithProviders(<DocumentList knowledgeBaseId="kb-1" />);
    await screen.findByText("行程.txt");
    fireEvent.click(screen.getByRole("button", { name: "详情" }));

    const drawer = await screen.findByRole("dialog", { name: "资料详情" });
    expect(within(drawer).getByText("行程.txt")).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: "关闭" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "资料详情" })).not.toBeInTheDocument()
    );

    fireEvent.click(screen.getByRole("button", { name: "详情" }));
    const reopenedDrawer = await screen.findByRole("dialog", { name: "资料详情" });
    fireEvent.click(within(reopenedDrawer).getByRole("button", { name: "删除" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(api.deleteDocument).toHaveBeenCalledWith("kb-1", "doc-1"));
    expect(screen.queryByRole("dialog", { name: "资料详情" })).not.toBeInTheDocument();
  });
});

describe("阶段 13 体验一致性（T154）", () => {
  it("上传接受后立即出现非终态资料行，不等轮询", async () => {
    const existing = documentFixture({ id: "doc-existing", filename: "已完成.txt" });
    const uploaded = documentFixture({
      id: "doc-new",
      filename: "稍后完成.txt",
      status: "processing",
      current_task_type: "parse",
    });
    api.listDocuments
      .mockResolvedValueOnce(page([existing]))
      .mockResolvedValueOnce(page([existing, uploaded]));
    api.uploadDocuments.mockResolvedValue({ documents: [uploaded] });

    renderWithProviders(<KnowledgeBaseDocumentsPage />);
    await screen.findByText("已完成.txt");
    selectUpload([new File(["later"], "稍后完成.txt", { type: "text/plain" })]);

    // 接受后立即重取当前页：processing 行无需等待轮询即可见（批次跟踪查询另计）。
    expect(await screen.findByText("稍后完成.txt")).toBeInTheDocument();
    expect(api.listDocuments.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("上传中再次选择文件被阻止并给出明确提示", async () => {
    api.listDocuments.mockResolvedValue(page([]));
    api.uploadDocuments.mockReturnValue(new Promise(() => undefined));

    renderWithProviders(<KnowledgeBaseDocumentsPage />);
    await screen.findByText("暂无资料");
    selectUpload([new File(["a"], "a.txt", { type: "text/plain" })]);

    // 上传进行中：输入被禁用并显示处理中提示，第二次选择无效。
    expect(await screen.findByText(/正在上传… 0%/)).toBeInTheDocument();
    expect(screen.getByLabelText("选择文件")).toBeDisabled();
    selectUpload([new File(["b"], "b.txt", { type: "text/plain" })]);
    expect(api.uploadDocuments).toHaveBeenCalledTimes(1);
  });

  it("读取失败显示可重试错误，不降级为空列表", async () => {
    api.listDocuments.mockRejectedValue({ msg: "资料加载失败", traceId: "trace-docs" });

    renderWithProviders(<DocumentList knowledgeBaseId="kb-1" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("资料加载失败");
    expect(screen.queryByText("暂无资料")).not.toBeInTheDocument();
  });

  it("资料页标题显示所属知识库名称", async () => {
    api.listDocuments.mockResolvedValue(page([]));
    api.getKnowledgeBase.mockResolvedValue(KNOWLEDGE_BASE);

    renderWithProviders(<KnowledgeBaseDocumentsPage />);

    expect(await screen.findByText(/旅行笔记/)).toBeInTheDocument();
    expect(api.getKnowledgeBase).toHaveBeenCalledWith("kb-1");
  });
});

describe("资料加载失败", () => {
  it("列表失败显示安全消息、trace_id 与显式重试", async () => {
    api.listDocuments.mockRejectedValueOnce(apiError("资料列表暂时无法加载"));
    api.listDocuments.mockResolvedValueOnce(page([documentFixture()]));

    renderWithProviders(<DocumentList knowledgeBaseId="kb-1" />);
    expect(await screen.findByText("资料列表暂时无法加载")).toBeInTheDocument();
    expect(screen.getByText(`trace_id: ${TRACE_ID}`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    expect(await screen.findByText("行程.txt")).toBeInTheDocument();
    expect(api.listDocuments).toHaveBeenCalledTimes(2);
  });

  it("详情失败显示安全消息、trace_id 与显式重试", async () => {
    api.listDocuments.mockResolvedValue(page([documentFixture()]));
    api.getDocument.mockRejectedValueOnce(apiError("资料详情暂时无法加载"));
    api.getDocument.mockResolvedValueOnce(documentFixture());

    renderWithProviders(<DocumentList knowledgeBaseId="kb-1" />);
    await screen.findByText("行程.txt");
    fireEvent.click(screen.getByRole("button", { name: "详情" }));

    expect(await screen.findByText("资料详情暂时无法加载")).toBeInTheDocument();
    expect(screen.getByText(`trace_id: ${TRACE_ID}`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    expect(await screen.findByRole("dialog", { name: "资料详情" })).toBeInTheDocument();
    expect(api.getDocument).toHaveBeenCalledTimes(2);
  });
});
