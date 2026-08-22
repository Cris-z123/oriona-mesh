import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/app-shell/AppShell";
import { AuthProvider } from "@/features/auth/AuthProvider";
import { RegisterForm } from "@/features/auth/RegisterForm";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { clearSession, getSession, setSession } from "@/lib/api/session";

/** T140 [US4]：核心认证路径的组件级红灯验收。 */
const TRACE_ID = "trace-auth-recovery-001";
const USER = { id: "u-alice", email: "alice@example.com", display_name: "Alice" };

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
    getMe: vi.fn(),
    logout: vi.fn(),
    register: vi.fn(),
  };
});

vi.mock("@/lib/api/client", () => api);

const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/knowledge-bases",
  useRouter: () => ({
    push: nav.push,
    replace: nav.replace,
    back: vi.fn(),
    forward: vi.fn(),
    refresh: vi.fn(),
    prefetch: vi.fn(),
  }),
  useSearchParams: () => new URLSearchParams(""),
}));

beforeEach(() => {
  clearSession();
  nav.push.mockReset();
  nav.replace.mockReset();
  api.getMe.mockReset();
  api.logout.mockReset();
  api.register.mockReset();
});

afterEach(() => {
  clearSession();
  vi.useRealTimers();
  vi.clearAllMocks();
  setViewportWidth(1280);
});

/** AppShell 在小于 ui-design §3.1 规定的 1024px 断点时显示抽屉入口。 */
function setViewportWidth(width: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width, writable: true });
  window.dispatchEvent(new Event("resize"));
}

function fillRegistration(password: string, confirmPassword = password) {
  fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "alice@example.com" } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: confirmPassword } });
}

function expectInvalidRegistrationField(fieldLabel: string, error: HTMLElement) {
  const field = screen.getByLabelText(fieldLabel);
  expect(field).toHaveAttribute("aria-invalid", "true");
  expect(error.id).not.toBe("");
  expect(field.getAttribute("aria-describedby")?.split(/\s+/)).toContain(error.id);
  // 密码字段还关联规则预先提示，可访问描述是提示与错误的组合。
  expect(field).toHaveAccessibleDescription(expect.stringContaining(error.textContent ?? ""));
  expect(error).toHaveAttribute("role", "alert");
}

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      "button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
    )
  ).filter((element) => element.getAttribute("aria-hidden") !== "true");
}

describe("注册校验", () => {
  it("密码字段展示规则预先提示（阶段 13，T156）", async () => {
    render(<RegisterForm />);

    const passwordField = screen.getByLabelText("密码");
    expect(passwordField).toHaveAccessibleDescription(expect.stringContaining("至少 8 个字符"));
    expect(passwordField).toHaveAccessibleDescription(expect.stringContaining("字母和数字"));
  });

  it("密码字段位于确认密码之前（阶段 13，T156）", async () => {
    render(<RegisterForm />);

    const password = screen.getByLabelText("密码");
    const confirm = screen.getByLabelText("确认密码");
    expect(password.compareDocumentPosition(confirm) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING
    );
  });

  it("确认密码不一致时在字段旁提示且不发送注册请求", async () => {
    render(<RegisterForm />);
    fillRegistration("pass1234", "pass5678");

    fireEvent.click(screen.getByRole("button", { name: "注册" }));

    const error = await screen.findByText("两次输入的密码不一致");
    expectInvalidRegistrationField("确认密码", error);
    expect(api.register).not.toHaveBeenCalled();
  });

  it.each(["abcdefgh", "12345678"])("密码 %s 未同时包含字母和数字时阻止提交", async (password) => {
    render(<RegisterForm />);
    fillRegistration(password);

    fireEvent.click(screen.getByRole("button", { name: "注册" }));

    const error = await screen.findByText("密码必须同时包含字母和数字");
    expectInvalidRegistrationField("密码", error);
    expect(api.register).not.toHaveBeenCalled();
  });

  it("密码少于 8 个字符时阻止提交", async () => {
    render(<RegisterForm />);
    fillRegistration("pass123");

    fireEvent.click(screen.getByRole("button", { name: "注册" }));

    const error = await screen.findByText("密码至少需要 8 个字符");
    expectInvalidRegistrationField("密码", error);
    expect(api.register).not.toHaveBeenCalled();
  });

  it("注册请求只发送 API 契约字段，不传输确认密码", async () => {
    api.register.mockResolvedValue(USER);
    render(<RegisterForm />);
    fillRegistration("pass1234");

    fireEvent.click(screen.getByRole("button", { name: "注册" }));

    await waitFor(() =>
      expect(api.register).toHaveBeenCalledWith({
        email: "alice@example.com",
        password: "pass1234",
      })
    );
    expect(api.register.mock.calls[0]?.[0]).not.toHaveProperty("confirmPassword");
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/login"));
  });
});

describe("认证恢复", () => {
  it("连续恢复失败时显示安全错误及 trace_id，并提供重试和退出入口", async () => {
    setSession({
      accessToken: "at-alice",
      refreshToken: "rt-alice",
      expiresAt: Date.now() + 60_000,
    });
    api.getMe.mockRejectedValue(
      new api.ApiError({ code: 50000, msg: "暂时无法恢复会话", status: 500, traceId: TRACE_ID })
    );

    render(
      <AuthProvider>
        <RequireAuth>
          <p>受保护内容</p>
        </RequireAuth>
      </AuthProvider>
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法恢复会话");
    expect(screen.getByText(`trace_id: ${TRACE_ID}`)).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "重试" });
    const attemptsBeforeRetry = api.getMe.mock.calls.length;
    fireEvent.click(retry);
    await act(async () => {
      await Promise.resolve();
    });
    expect(api.getMe.mock.calls.length).toBeGreaterThan(attemptsBeforeRetry);

    fireEvent.click(screen.getByRole("button", { name: "退出登录" }));
    await waitFor(() => expect(getSession()).toBeNull());
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/login"));
    expect(screen.queryByText("受保护内容")).not.toBeInTheDocument();
  });
});

describe("表单安全错误", () => {
  it("注册服务端错误紧邻表单呈现安全消息与可复制 trace_id", async () => {
    api.register.mockRejectedValue(
      new api.ApiError({ code: 20001, msg: "邮箱已注册", status: 409, traceId: TRACE_ID })
    );
    render(<RegisterForm />);
    fillRegistration("pass1234");

    fireEvent.click(screen.getByRole("button", { name: "注册" }));

    const form = screen.getByRole("button", { name: "注册" }).closest("form");
    expect(form).not.toBeNull();
    const error = await within(form as HTMLFormElement).findByRole("alert");
    expect(error).toHaveTextContent("邮箱已注册");
    expect(error).toHaveTextContent(`trace_id: ${TRACE_ID}`);
    expect(screen.getByRole("button", { name: "复制追踪 ID" })).toBeInTheDocument();
  });
});

describe("账户菜单与窄屏导航", () => {
  it("显示名派生首字母账户菜单，并提供资料、主题和退出操作", async () => {
    const user = userEvent.setup();
    setSession({
      accessToken: "at-alice",
      refreshToken: "rt-alice",
      expiresAt: Date.now() + 60_000,
    });
    api.getMe.mockResolvedValue(USER);
    render(
      <AuthProvider>
        <AppShell>内容</AppShell>
      </AuthProvider>
    );

    const accountMenu = await screen.findByRole("button", { name: /账户菜单/ });
    expect(accountMenu).toHaveTextContent("A");
    accountMenu.focus();
    await user.keyboard("{Enter}");

    const menu = await screen.findByRole("menu");
    expect(screen.getByRole("menuitem", { name: "个人资料" })).toHaveAttribute("href", "/profile");
    expect(screen.getByRole("menuitem", { name: /切换到.*主题/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "退出登录" })).toBeInTheDocument();
    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(accountMenu);
  });

  it("窄屏导航抽屉保留导航与全部账户等价入口", async () => {
    const user = userEvent.setup();
    setViewportWidth(1023);
    render(
      <AuthProvider>
        <AppShell>内容</AppShell>
      </AuthProvider>
    );

    const trigger = screen.getByRole("button", { name: "打开导航" });
    trigger.focus();
    await user.keyboard("{Enter}");
    const drawer = await screen.findByRole("dialog", { name: "OrionaMesh" });
    expect(drawer.contains(document.activeElement)).toBe(true);

    // 知识库选择与新建对话位于会话页面内容区（有意布局）；抽屉保留导航与账户入口。
    expect(within(drawer).getByRole("link", { name: "知识库" })).toBeInTheDocument();
    expect(within(drawer).getByRole("link", { name: "对话" })).toBeInTheDocument();
    fireEvent.click(within(drawer).getByRole("button", { name: "账户菜单" }));
    expect(within(drawer).getByRole("menuitem", { name: "个人资料" })).toBeInTheDocument();
    expect(within(drawer).getByRole("menuitem", { name: /切换到.*主题/ })).toBeInTheDocument();
    expect(within(drawer).getByRole("menuitem", { name: "退出登录" })).toBeInTheDocument();

    const focusables = focusableElements(drawer);
    expect(focusables.length).toBeGreaterThan(1);
    const firstFocusable = focusables[0];
    const lastFocusable = focusables.at(-1);
    expect(lastFocusable).toBeDefined();
    lastFocusable?.focus();
    await user.tab();
    expect(document.activeElement).toBe(firstFocusable);
    firstFocusable.focus();
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(lastFocusable);
    fireEvent.keyDown(drawer, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(trigger);
  });
});
