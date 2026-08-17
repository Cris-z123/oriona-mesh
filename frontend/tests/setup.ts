import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// vitest 未开启 globals，RTL 不会自动 cleanup：统一在每例后清理，避免跨用例 DOM 泄漏。
afterEach(() => {
  cleanup();
});
