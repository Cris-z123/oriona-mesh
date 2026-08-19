import { describe, expect, it, vi } from "vitest";

const redirect = vi.hoisted(() => vi.fn());

vi.mock("next/navigation", () => ({ redirect }));

import HomePage from "@/app/page";

describe("首页", () => {
  it("在服务端直接重定向到知识库页", () => {
    HomePage();

    expect(redirect).toHaveBeenCalledWith("/knowledge-bases");
  });
});
