import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../helpers";
import { AuthProvider, useAuth } from "@/features/auth/AuthProvider";
import { LoginForm } from "@/features/auth/LoginForm";
import { RegisterForm } from "@/features/auth/RegisterForm";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { DocumentDetail } from "@/features/documents/DocumentDetail";
import { DocumentList } from "@/features/documents/DocumentList";
import { UploadPanel } from "@/features/documents/UploadPanel";
import { KnowledgeBaseList } from "@/features/knowledge-bases/KnowledgeBaseList";
import { ProfileForm } from "@/features/profile/ProfileForm";
import { clearSession, setSession } from "@/lib/api/session";

/**
 * T107 [P] [US1] 用户故事 1 组件测试。
 *
 * 覆盖：认证（登录/注册/会话恢复/受保护路由）、本人基本资料查看与更新、
 * 知识库列表与 delete_failed/20015 最小墓碑/重试删除、上传限制提示与幂等重放 409、
 * 资料状态轮询终态、失败资料删除、allowed_actions 渲染（不得自行推导）与
 * 404 不可见资源提示（含 trace_id）。
 */
const TEST_TRACE_ID = "7eb23f43-e1f4-4a67-a64d-1a481b36030f";

const api = vi.hoisted(() => {
  class ApiError extends Error {
    code: number;
    status: number;
    msg: string;
    traceId: string | null;
    retryAfter: number | null;
    constructor(params: {
      code: number;
      msg: string;
      status: number;
      traceId: string | null;
      retryAfter?: number | null;
    }) {
      super(params.msg);
      this.name = "ApiError";
      this.code = params.code;
      this.status = params.status;
      this.msg = params.msg;
      this.traceId = params.traceId;
      this.retryAfter = params.retryAfter ?? null;
    }
  }
  return {
    ApiError,
    asApiError: (err: unknown) =>
      err instanceof ApiError
        ? err
        : new ApiError({ code: 50000, msg: "系统繁忙，请稍后再试", status: 0, traceId: null }),
    register: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
    getMe: vi.fn(),
    updateMe: vi.fn(),
    listKnowledgeBases: vi.fn(),
    createKnowledgeBase: vi.fn(),
    updateKnowledgeBase: vi.fn(),
    deleteKnowledgeBase: vi.fn(),
    listDocuments: vi.fn(),
    getDocument: vi.fn(),
    listDocumentTasks: vi.fn(),
    deleteDocument: vi.fn(),
    uploadDocuments: vi.fn(),
    generateIdempotencyKey: vi.fn(() => "auto-key-0001"),
  };
});

vi.mock("@/lib/api/client", () => api);

const nav = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  pathname: "/",
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: nav.push,
    replace: nav.replace,
    back: vi.fn(),
    forward: vi.fn(),
    refresh: vi.fn(),
    prefetch: vi.fn(),
  }),
  usePathname: () => nav.pathname,
  useSearchParams: () => new URLSearchParams(),
}));

function apiError(code: number, msg: string, status = 400, traceId = TEST_TRACE_ID) {
  return new api.ApiError({ code, msg, status, traceId });
}

const USER = { id: "u1", email: "a@example.com", display_name: "Alice" };

const KB_ACTIVE = {
  id: "kb-1",
  name: "笔记",
  description: "日常笔记",
  status: "active",
  delete_error_code: null,
  allowed_actions: ["delete"],
  created_at: "2026-08-18T00:00:00Z",
  updated_at: "2026-08-18T00:00:00Z",
};

const KB_TOMBSTONE = {
  id: "kb-2",
  name: null,
  description: null,
  status: "delete_failed",
  delete_error_code: 20015,
  allowed_actions: ["retry_delete"],
  created_at: "2026-08-18T00:00:00Z",
  updated_at: "2026-08-18T00:00:00Z",
};

function doc(overrides: Record<string, unknown> = {}) {
  return {
    id: "d1",
    knowledge_base_id: "kb-1",
    filename: "a.txt",
    file_type: "txt",
    file_size: 10,
    status: "queued",
    version: 1,
    current_task_type: "parse",
    retry_count: 0,
    delete_cycle: 0,
    chunk_count: 0,
    error_code: null,
    error_message: null,
    processing_started_at: null,
    processing_finished_at: null,
    created_at: "2026-08-18T00:00:00Z",
    updated_at: "2026-08-18T00:00:00Z",
    allowed_actions: ["delete"],
    ...overrides,
  };
}

const DOC_FAILED = doc({
  id: "d-failed",
  filename: "bad.pdf",
  status: "failed",
  error_code: 20001,
  error_message: "资料解析失败，请删除后重新上传",
});
const DOC_EMPTY = doc({
  id: "d-empty",
  filename: "blank.md",
  status: "failed",
  error_code: 20010,
  error_message: "资料内容为空，请删除后重新上传",
});
const DOC_TOMBSTONE = doc({
  id: "d-tomb",
  filename: "gone.pdf",
  status: "failed",
  current_task_type: "delete_cleanup",
  error_code: 20015,
  error_message: "资料删除未完成，请重试删除",
  allowed_actions: ["retry_delete"],
});
const DOC_COMPLETED = doc({
  id: "d-done",
  filename: "ok.pdf",
  status: "completed",
  current_task_type: null,
});

const page = (items: unknown[], total = items.length) => ({
  items,
  page: 1,
  page_size: 20,
  total,
});

beforeEach(() => {
  clearSession();
  nav.push.mockReset();
  nav.replace.mockReset();
  nav.pathname = "/";
  for (const fn of Object.values(api)) {
    if (typeof fn === "function" && "mockReset" in fn) (fn as ReturnType<typeof vi.fn>).mockReset();
  }
  api.generateIdempotencyKey.mockReturnValue("auto-key-0001");
});

afterEach(() => {
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// 认证：登录 / 注册 / 会话恢复 / 受保护路由
// ---------------------------------------------------------------------------

describe("US1 认证", () => {
  it("登录成功调用 API 并跳转知识库", async () => {
    api.login.mockResolvedValue({
      access_token: "at-1",
      refresh_token: "rt_1",
      token_type: "Bearer",
      expires_in: 7200,
    });
    render(<LoginForm />);
    fireEvent.change(screen.getByLabelText(/邮箱/), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByLabelText(/密码/), { target: { value: "pw-123456" } });
    fireEvent.click(screen.getByRole("button", { name: /登录/ }));
    await waitFor(() =>
      expect(api.login).toHaveBeenCalledWith({ email: "a@example.com", password: "pw-123456" })
    );
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/knowledge-bases"));
  });

  it("登录失败显示服务端业务错误信息", async () => {
    api.login.mockRejectedValue(apiError(10004, "邮箱或密码错误", 401));
    render(<LoginForm />);
    fireEvent.change(screen.getByLabelText(/邮箱/), { target: { value: "a@example.com" } });
    fireEvent.change(screen.getByLabelText(/密码/), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: /登录/ }));
    expect(await screen.findByText("邮箱或密码错误")).toBeInTheDocument();
  });

  it("注册成功跳转登录页", async () => {
    api.register.mockResolvedValue(USER);
    render(<RegisterForm />);
    fireEvent.change(screen.getByLabelText(/邮箱/), { target: { value: "a@example.com" } });
    // 精确 label：确认密码与密码两个字段（阶段 12 新增确认密码）。
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "pw-123456" } });
    fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "pw-123456" } });
    fireEvent.change(screen.getByLabelText(/昵称/), { target: { value: "Alice" } });
    fireEvent.click(screen.getByRole("button", { name: /注册/ }));
    await waitFor(() =>
      expect(api.register).toHaveBeenCalledWith({
        email: "a@example.com",
        password: "pw-123456",
        display_name: "Alice",
      })
    );
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/login"));
  });

  it("已有会话时恢复用户并可访问受保护内容", async () => {
    setSession({ accessToken: "at-1", refreshToken: "rt_1", expiresAt: Date.now() + 100000 });
    api.getMe.mockResolvedValue(USER);
    render(
      <AuthProvider>
        <RequireAuth>
          <div>受保护内容</div>
        </RequireAuth>
      </AuthProvider>
    );
    expect(await screen.findByText("受保护内容")).toBeInTheDocument();
    await waitFor(() => expect(api.getMe).toHaveBeenCalled());
  });

  it("未登录访问受保护路由重定向到登录页", async () => {
    render(
      <AuthProvider>
        <RequireAuth>
          <div>受保护内容</div>
        </RequireAuth>
      </AuthProvider>
    );
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/login"));
  });

  it("会话失效后（10001 恢复失败）清除会话并重定向", async () => {
    setSession({ accessToken: "at-expired", refreshToken: "rt_1", expiresAt: Date.now() - 1000 });
    api.getMe.mockRejectedValue(apiError(10001, "请重新登录", 401));
    render(
      <AuthProvider>
        <RequireAuth>
          <div>受保护内容</div>
        </RequireAuth>
      </AuthProvider>
    );
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/login"));
  });

  it("切换会话时不显示上一账号资料，直到新会话恢复完成", async () => {
    const userB = { id: "u2", email: "b@example.com", display_name: "Bob" };
    let resolveUserB: ((user: typeof userB) => void) | undefined;
    api.getMe.mockResolvedValueOnce(USER).mockImplementationOnce(
      () =>
        new Promise<typeof userB>((resolve) => {
          resolveUserB = resolve;
        })
    );

    function AuthProbe() {
      const { ready, user } = useAuth();
      return <p>{ready ? (user?.email ?? "无用户") : "恢复中"}</p>;
    }

    setSession({ accessToken: "at-a", refreshToken: "rt-a", expiresAt: Date.now() + 100000 });
    render(
      <AuthProvider>
        <AuthProbe />
      </AuthProvider>
    );
    expect(await screen.findByText("a@example.com")).toBeInTheDocument();

    act(() => {
      setSession({ accessToken: "at-b", refreshToken: "rt-b", expiresAt: Date.now() + 100000 });
    });
    expect(screen.getByText("恢复中")).toBeInTheDocument();
    expect(screen.queryByText("a@example.com")).not.toBeInTheDocument();

    await act(async () => {
      resolveUserB?.(userB);
    });
    expect(await screen.findByText("b@example.com")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 本人基本资料（FR-002）
// ---------------------------------------------------------------------------

describe("US1 个人资料", () => {
  it("展示当前显示名并可更新", async () => {
    setSession({ accessToken: "at-1", refreshToken: "rt_1", expiresAt: Date.now() + 100000 });
    api.getMe.mockResolvedValue(USER);
    api.updateMe.mockResolvedValue({ ...USER, display_name: "Bob" });
    render(
      <AuthProvider>
        <ProfileForm />
        <CurrentUserProbe />
      </AuthProvider>
    );
    const input = await screen.findByLabelText(/显示名/);
    expect(input).toHaveValue("Alice");
    fireEvent.change(input, { target: { value: "Bob" } });
    fireEvent.click(screen.getByRole("button", { name: /保存/ }));
    await waitFor(() => expect(api.updateMe).toHaveBeenCalledWith({ display_name: "Bob" }));
    await waitFor(() => expect(screen.getByLabelText(/显示名/)).toHaveValue("Bob"));
    expect(screen.getByText("当前用户：Bob")).toBeInTheDocument();
  });
});

function CurrentUserProbe() {
  const { user } = useAuth();
  return <p>当前用户：{user?.display_name ?? "未加载"}</p>;
}

// ---------------------------------------------------------------------------
// 知识库列表 / 删除墓碑 / 重试删除（FR-003）
// ---------------------------------------------------------------------------

describe("US1 知识库列表", () => {
  it("渲染 active 知识库与 delete_failed 最小墓碑，墓碑不显示名称/描述", async () => {
    api.listKnowledgeBases.mockResolvedValue(page([KB_ACTIVE, KB_TOMBSTONE]));
    renderWithProviders(<KnowledgeBaseList />);
    expect(await screen.findByText("笔记")).toBeInTheDocument();
    expect(screen.getByText("日常笔记")).toBeInTheDocument();
    // 墓碑：仅最小“删除未完成”与重试删除；不得显示名称/描述或子资源入口
    expect(screen.getByText("删除未完成")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重试删除/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /打开知识库/ })).not.toBeInTheDocument();
  });

  it("active 知识库仅允许删除操作；delete_failed 重试删除调用 DELETE", async () => {
    // 删除成功后列表重取：第二次返回相同数据，保证墓碑行在断言期间仍存在
    api.listKnowledgeBases
      .mockResolvedValueOnce(page([KB_ACTIVE, KB_TOMBSTONE]))
      .mockResolvedValueOnce(page([KB_ACTIVE, KB_TOMBSTONE]));
    api.deleteKnowledgeBase.mockResolvedValue(undefined);
    renderWithProviders(<KnowledgeBaseList />);
    await screen.findByText("笔记");
    // active 行：仅“删除”
    fireEvent.click(screen.getByRole("button", { name: /删除笔记/ }));
    fireEvent.click(screen.getByRole("button", { name: /^确认删除$/ }));
    await waitFor(() => expect(api.deleteKnowledgeBase).toHaveBeenCalledWith("kb-1"));
    // delete_failed 行：仅“重试删除”，重复请求不显示普通删除
    fireEvent.click(screen.getByRole("button", { name: /重试删除/ }));
    fireEvent.click(screen.getByRole("button", { name: /^确认重试删除$/ }));
    await waitFor(() => expect(api.deleteKnowledgeBase).toHaveBeenCalledWith("kb-2"));
  });

  it("创建知识库后导航进入其资料工作区并刷新列表", async () => {
    api.listKnowledgeBases.mockResolvedValue(page([KB_ACTIVE]));
    api.createKnowledgeBase.mockResolvedValue(KB_ACTIVE);
    renderWithProviders(<KnowledgeBaseList />);
    await screen.findByText("笔记");
    fireEvent.change(screen.getByLabelText(/知识库名称/), { target: { value: "新库" } });
    fireEvent.click(screen.getByRole("button", { name: /创建/ }));
    await waitFor(() => expect(api.createKnowledgeBase).toHaveBeenCalledWith({ name: "新库" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/knowledge-bases/${KB_ACTIVE.id}`));
    await waitFor(() => expect(api.listKnowledgeBases).toHaveBeenCalledTimes(2));
  });
});

// ---------------------------------------------------------------------------
// 上传面板：限制提示 / 幂等键 / 进度 / 协调 409（FR-004/024/025/031）
// ---------------------------------------------------------------------------

describe("US1 上传面板", () => {
  function upload(files: File[]) {
    fireEvent.change(screen.getByLabelText(/选择文件/), { target: { files } });
  }

  it("超过 20 个文件显示数量限制提示且不发起上传", async () => {
    api.uploadDocuments.mockResolvedValue({ documents: [] });
    render(<UploadPanel knowledgeBaseId="kb-1" />);
    const files = Array.from(
      { length: 21 },
      (_, i) => new File(["x"], `f${i}.txt`, { type: "text/plain" })
    );
    upload(files);
    expect(await screen.findByText("单次上传最多 20 个文件")).toBeInTheDocument();
    expect(api.uploadDocuments).not.toHaveBeenCalled();
  });

  it("超过 50MB 的文件显示大小限制提示", async () => {
    render(<UploadPanel knowledgeBaseId="kb-1" />);
    const big = new File(["x"], "big.pdf", { type: "application/pdf" });
    Object.defineProperty(big, "size", { value: 50 * 1024 * 1024 + 1 });
    upload([big]);
    expect(await screen.findByText("文件超过 50MB 限制")).toBeInTheDocument();
  });

  it("不支持的文件类型显示格式提示", async () => {
    render(<UploadPanel knowledgeBaseId="kb-1" />);
    upload([new File(["x"], "bad.exe", { type: "application/x-msdownload" })]);
    expect(await screen.findByText("仅支持 PDF、DOCX、MD 和 TXT 文件")).toBeInTheDocument();
  });

  it("合法文件自动生成幂等键并上传，进度回调渲染百分比", async () => {
    api.uploadDocuments.mockImplementation((kb, files, opts) => {
      void kb;
      void files;
      // 先报告进度（t=0），稍后再完成请求：让中间进度状态可被渲染断言
      opts?.onProgress?.(50, 100);
      return new Promise((resolve) => {
        setTimeout(() => resolve({ documents: [doc()] }), 50);
      });
    });
    const onUploaded = vi.fn();
    render(<UploadPanel knowledgeBaseId="kb-1" onUploaded={onUploaded} />);
    upload([new File(["hello"], "a.txt", { type: "text/plain" })]);
    await waitFor(() =>
      expect(api.uploadDocuments).toHaveBeenCalledWith(
        "kb-1",
        [expect.any(File)],
        expect.objectContaining({ idempotencyKey: "auto-key-0001" })
      )
    );
    expect(await screen.findByText(/正在上传… 50%/)).toBeInTheDocument();
    await waitFor(() => expect(onUploaded).toHaveBeenCalled());
  });

  it("同键重放协调中 20008/409 显示冲突信息", async () => {
    api.uploadDocuments.mockRejectedValue(apiError(20008, "请求与当前资源状态冲突", 409));
    render(<UploadPanel knowledgeBaseId="kb-1" />);
    upload([new File(["hello"], "a.txt", { type: "text/plain" })]);
    expect(await screen.findByText("请求与当前资源状态冲突")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// 资料列表：轮询终态 / 失败原因 / allowed_actions / 20015 墓碑（FR-005/008a/010/011）
// ---------------------------------------------------------------------------

describe("US1 资料列表", () => {
  it("轮询直到资料达到终态", async () => {
    api.listDocuments.mockResolvedValueOnce(page([doc({ status: "processing" })]));
    api.listDocuments.mockResolvedValueOnce(page([DOC_COMPLETED]));
    renderWithProviders(<DocumentList knowledgeBaseId="kb-1" pollIntervalMs={200} />);
    // 状态以徽章文本呈现（Radix Select 选项在打开前不进入 DOM，无需 selector 限定）
    expect(await screen.findByText("处理中")).toBeInTheDocument();
    expect(await screen.findByText("已完成")).toBeInTheDocument();
    expect(api.listDocuments).toHaveBeenCalledTimes(2);
  });

  it("失败资料显示服务端失败原因与删除操作；空文档删除后列表刷新", async () => {
    api.listDocuments.mockResolvedValueOnce(page([DOC_FAILED, DOC_EMPTY]));
    api.listDocuments.mockResolvedValueOnce(page([]));
    api.deleteDocument.mockResolvedValue(undefined);
    renderWithProviders(<DocumentList knowledgeBaseId="kb-1" />);
    expect(await screen.findByText("资料解析失败，请删除后重新上传")).toBeInTheDocument();
    expect(screen.getByText("资料内容为空，请删除后重新上传")).toBeInTheDocument();
    const deleteButtons = screen.getAllByRole("button", { name: /^删除$/ });
    expect(deleteButtons).toHaveLength(2);
    fireEvent.click(deleteButtons[0]!);
    fireEvent.click(screen.getByRole("button", { name: /^确认删除$/ }));
    await waitFor(() => expect(api.deleteDocument).toHaveBeenCalledWith("kb-1", "d-failed"));
    await waitFor(() => expect(api.listDocuments).toHaveBeenCalledTimes(2));
  });

  it("删除末页最后一项后回退到上一页，不停留在空页", async () => {
    api.listDocuments.mockResolvedValueOnce(page([DOC_COMPLETED], 21)); // 第 1 页
    api.listDocuments.mockResolvedValueOnce(page([DOC_COMPLETED], 21)); // 第 2 页（仅 1 项）
    api.listDocuments.mockResolvedValueOnce(page([], 21)); // 回退后的第 1 页
    api.deleteDocument.mockResolvedValue(undefined);
    renderWithProviders(<DocumentList knowledgeBaseId="kb-1" />);
    await screen.findByText("ok.pdf");
    fireEvent.click(screen.getByRole("button", { name: /下一页/ }));
    await waitFor(() => expect(api.listDocuments).toHaveBeenCalledTimes(2));
    // 第 2 页上删除最后一项
    fireEvent.click(screen.getByRole("button", { name: /^删除$/ }));
    fireEvent.click(screen.getByRole("button", { name: /^确认删除$/ }));
    await waitFor(() => expect(api.deleteDocument).toHaveBeenCalledWith("kb-1", "d-done"));
    await waitFor(() => expect(api.listDocuments).toHaveBeenCalledTimes(3));
    // 回退请求携带 page=1
    expect(api.listDocuments.mock.calls[2]).toEqual(["kb-1", 1, 20, undefined]);
  });

  it("failed/delete_cleanup/20015 仅显示最小墓碑与重试删除，不作为普通失败资料", async () => {
    api.listDocuments.mockResolvedValue(page([DOC_TOMBSTONE]));
    api.deleteDocument.mockResolvedValue(undefined);
    renderWithProviders(<DocumentList knowledgeBaseId="kb-1" />);
    expect(await screen.findByText("资料删除未完成，请重试删除")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: /重试删除/ });
    expect(screen.queryByRole("button", { name: /^删除$/ })).not.toBeInTheDocument();
    fireEvent.click(retry);
    fireEvent.click(screen.getByRole("button", { name: /^确认重试删除$/ }));
    await waitFor(() => expect(api.deleteDocument).toHaveBeenCalledWith("kb-1", "d-tomb"));
  });

  it("详情展示完整 DTO 与 allowed_actions；隐藏资源 404 显示服务端提示与 trace_id", async () => {
    api.getDocument
      .mockResolvedValueOnce(DOC_COMPLETED)
      .mockRejectedValueOnce(apiError(20007, "请求的资源不存在", 404));
    renderWithProviders(<DocumentDetail knowledgeBaseId="kb-1" documentId="d-done" />);
    expect(await screen.findByText("ok.pdf")).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /删除/ })).toBeInTheDocument();
    // 切换到不可见资源：404 提示与 trace_id
    renderWithProviders(<DocumentDetail knowledgeBaseId="kb-1" documentId="d-hidden" />);
    expect(await screen.findByText("请求的资源不存在")).toBeInTheDocument();
    expect(screen.getByText(`trace_id: ${TEST_TRACE_ID}`)).toBeInTheDocument();
  });

  it("资料删除成功后关闭详情，不以 404 重取保留旧资料", async () => {
    api.getDocument.mockResolvedValue(DOC_COMPLETED);
    api.listDocumentTasks.mockResolvedValue(page([]));
    api.deleteDocument.mockResolvedValue(undefined);
    const onClose = vi.fn();

    renderWithProviders(
      <DocumentDetail knowledgeBaseId="kb-1" documentId="d-done" onClose={onClose} />
    );
    await screen.findByText("ok.pdf");
    fireEvent.click(screen.getByRole("button", { name: /^删除$/ }));
    fireEvent.click(screen.getByRole("button", { name: /^确认删除$/ }));

    await waitFor(() => expect(api.deleteDocument).toHaveBeenCalledWith("kb-1", "d-done"));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(api.getDocument).toHaveBeenCalledTimes(1);
  });
});
