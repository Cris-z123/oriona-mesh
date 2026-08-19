import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { ThemeProvider } from "next-themes";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/app-shell/AppShell";
import { ThemeToggle } from "@/components/app-shell/ThemeToggle";
import { ErrorState } from "@/components/ui/error-state";
import { EmptyState } from "@/components/ui/empty-state";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { AuthProvider } from "@/features/auth/AuthProvider";
import { ApiError } from "@/lib/api/client";
import { useUiStore } from "@/stores/ui-store";
import type { ReactNode } from "react";

/**
 * T134 [P] 前端共享 UI 基础组件测试（先写后验）。
 *
 * 覆盖：语义主题令牌（浅/深色 + prefers-reduced-motion）、桌面应用壳与键盘导航、
 * 抽屉焦点回归（Sheet 打开/焦点圈定/Escape 关闭/焦点回归）、
 * EmptyState/ErrorState 与 ThemeToggle 主题切换。
 */
const TEST_TRACE_ID = "7eb23f43-e1f4-4a67-a64d-1a481b36030f";

vi.mock("next/navigation", () => ({
  usePathname: () => "/knowledge-bases",
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    refresh: vi.fn(),
    prefetch: vi.fn(),
  }),
}));

beforeEach(() => {
  // 每个用例恢复 UI store 初始状态，避免跨用例泄漏
  useUiStore.setState({
    navCollapsed: false,
    citationDrawerSelector: null,
    documentStatusFilter: "all",
  });
  document.documentElement.classList.remove("dark");
});

// ---------------------------------------------------------------------------
// 语义主题令牌（ui-design §2.1）与动效降级（§2.2）
// ---------------------------------------------------------------------------

describe("主题令牌与动效降级", () => {
  function cssFile(): string {
    return readFileSync(resolve(process.cwd(), "src/app/globals.css"), "utf8");
  }

  it("浅色为默认主题，且浅/深色都定义完整语义令牌", () => {
    const css = cssFile();
    // 默认浅色：:root 使用 ui-design 2.1 表浅色值
    expect(css).toContain("--background: #f7f6f1");
    expect(css).toContain("--surface: #fffefa");
    expect(css).toContain("--foreground: #202625");
    expect(css).toContain("--primary: #0c625d");
    expect(css).toContain("--clue: #534ca5");
    expect(css).toContain("--muted-foreground: #737b78");
    expect(css).toContain("--border: #dce3df");
    expect(css).toMatch(/--destructive:\s*#[0-9a-f]{6}/i);
    // 深色“夜间编辑桌”：同一角色延续
    expect(css).toMatch(/\.dark\s*\{/);
    expect(css).toContain("--background: #141918");
    expect(css).toContain("--surface: #1b2220");
    expect(css).toContain("--foreground: #e6ece8");
    expect(css).toContain("--primary: #4cae9c");
    expect(css).toContain("--clue: #aaa4ed");
    expect(css).toContain("--border: #31403b");
    // 深色变体由 next-themes 的 .dark 类驱动
    expect(css).toContain("&:where(.dark, .dark *)");
  });

  it("尊重 prefers-reduced-motion，动效不传达关键信息", () => {
    const css = cssFile();
    expect(css).toMatch(/@media\s*\(prefers-reduced-motion:\s*reduce\)/);
    expect(css).toContain("transition-duration: 0.01ms !important");
  });

  it("ThemeToggle 默认浅色，点击切换深色并更新可访问名称", () => {
    render(
      <ThemeProvider attribute="class" defaultTheme="light" enableSystem={false}>
        <ThemeToggle />
      </ThemeProvider>
    );
    const toggle = screen.getByRole("button", { name: "切换到深色主题" });
    expect(document.documentElement).not.toHaveClass("dark");
    fireEvent.click(toggle);
    expect(document.documentElement).toHaveClass("dark");
    const back = screen.getByRole("button", { name: "切换到浅色主题" });
    fireEvent.click(back);
    expect(document.documentElement).not.toHaveClass("dark");
  });
});

// ---------------------------------------------------------------------------
// 桌面应用壳：三层结构 / 键盘导航 / 折叠不丢失可访问名称
// ---------------------------------------------------------------------------

/** AppShell 内含 SignOutButton，需要 AuthProvider（无会话时可直接渲染）。 */
function renderShell(shell: ReactNode) {
  return render(<AuthProvider>{shell}</AuthProvider>);
}

describe("AppShell 桌面应用壳与键盘导航", () => {
  it("渲染工作区导航、主内容与上下文栏三层结构", () => {
    renderShell(
      <AppShell contextRail={<p>上下文内容</p>}>
        <h1>主内容</h1>
      </AppShell>
    );
    expect(screen.getByRole("navigation", { name: "工作区导航" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveTextContent("主内容");
    // 同一节点在桌面固定侧栏与小视口页面区域之间切换，不得重复挂载内容。
    expect(screen.getAllByText("上下文内容")).toHaveLength(1);
  });

  it("导航链接按视觉顺序排列且当前页有 aria-current", () => {
    renderShell(<AppShell>内容</AppShell>);
    // 品牌链接位于侧栏但不在 <nav> 地标内：按侧栏作用域断言视觉顺序
    const aside = screen.getByRole("complementary", { name: "工作区导航侧栏" });
    const links = within(aside).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["/", "/knowledge-bases", "/profile"]);
    expect(screen.getByRole("link", { name: "知识库" })).toHaveAttribute("aria-current", "page");
  });

  it("折叠导航后链接保留可访问名称，切换按钮反映展开状态", () => {
    renderShell(<AppShell>内容</AppShell>);
    const nav = screen.getByRole("navigation", { name: "工作区导航" });
    const collapse = screen.getByRole("button", { name: "折叠导航" });
    expect(collapse).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(collapse);
    // 折叠后：可访问名称仍存在（aria-label），可见文本隐藏
    expect(screen.getByRole("button", { name: "展开导航" })).toHaveAttribute(
      "aria-expanded",
      "false"
    );
    const navLink = within(nav).getByRole("link", { name: "知识库" });
    expect(navLink).toHaveAttribute("href", "/knowledge-bases");
    expect(screen.queryByText("知识库")).not.toBeInTheDocument();
    // 再次点击展开恢复
    fireEvent.click(screen.getByRole("button", { name: "展开导航" }));
    expect(within(nav).getByRole("link", { name: "知识库" })).toHaveTextContent("知识库");
  });
});

// ---------------------------------------------------------------------------
// 抽屉焦点回归（ui-design §7：焦点圈定、Escape 关闭、焦点回归）
// ---------------------------------------------------------------------------

describe("Sheet 抽屉焦点行为", () => {
  function DrawerHarness() {
    return (
      <Sheet>
        <SheetTrigger asChild>
          <button type="button">打开抽屉</button>
        </SheetTrigger>
        <SheetContent side="right">
          <SheetTitle>引用详情</SheetTitle>
          <button type="button">抽屉内按钮</button>
        </SheetContent>
      </Sheet>
    );
  }

  it("打开后焦点进入抽屉，Escape 关闭后焦点回归触发元素", async () => {
    render(<DrawerHarness />);
    const trigger = screen.getByRole("button", { name: "打开抽屉" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "引用详情" });
    // 模态语义：Radix 1.1.x 以 aria-hidden 隐藏其余内容（不再输出 aria-modal）
    expect(dialog.getAttribute("aria-hidden")).toBeNull();
    expect(screen.getByText("抽屉内按钮")).toBeInTheDocument();
    // 焦点圈定：激活元素位于抽屉内部
    expect(dialog.contains(document.activeElement)).toBe(true);
    // Escape 关闭
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // 焦点回归触发元素（Radix onCloseAutoFocus 默认聚焦 triggerRef）
    expect(document.activeElement).toBe(trigger);
  });
});

// ---------------------------------------------------------------------------
// 反馈组件（ui-design §5：空列表与可恢复错误；错误保留可复制 trace_id）
// ---------------------------------------------------------------------------

describe("EmptyState / ErrorState", () => {
  it("EmptyState 渲染标题、描述与可选操作", () => {
    render(
      <EmptyState title="暂无资料" description="上传第一份资料" action={<button>上传</button>} />
    );
    expect(screen.getByText("暂无资料")).toBeInTheDocument();
    expect(screen.getByText("上传第一份资料")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上传" })).toBeInTheDocument();
  });

  it("ErrorState 以 alert 呈现服务端 msg 与可复制 trace_id，不展示令牌或堆栈", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const error = new ApiError({
      code: 20007,
      msg: "请求的资源不存在",
      status: 404,
      traceId: TEST_TRACE_ID,
    });
    render(<ErrorState error={error} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("请求的资源不存在")).toBeInTheDocument();
    expect(screen.getByText(`trace_id: ${TEST_TRACE_ID}`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "复制追踪 ID" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(TEST_TRACE_ID));
    expect(screen.getByText("已复制追踪 ID")).toBeInTheDocument();
    expect(screen.queryByText(/Bearer|access|refresh/i)).not.toBeInTheDocument();
  });
});
