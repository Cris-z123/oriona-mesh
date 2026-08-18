import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// vitest 未开启 globals，RTL 不会自动 cleanup：统一在每例后清理，避免跨用例 DOM 泄漏。
afterEach(() => {
  cleanup();
});

/**
 * jsdom 未实现 matchMedia：next-themes（系统主题解析）与 prefers-reduced-motion
 * 相关代码需要稳定的可编程实现（ui-design §2.2/§7）。测试中默认匹配 false，
 * 需要模拟系统偏好时在用例内覆写 `window.matchMedia`。
 */
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string): MediaQueryList => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});
