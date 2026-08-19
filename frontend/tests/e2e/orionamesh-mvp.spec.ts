import { expect, test } from "@playwright/test";

import {
  apiError,
  apiFixtures,
  installApiMock,
  sseErrorFrame,
  sseFrame,
  sseResponse,
} from "./fixtures/api";

const pageOf = <T>(items: T[]) => ({ items, page: 1, page_size: 20, total: items.length });

async function restoreSession(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    localStorage.setItem(
      "orionamesh.session.v1",
      JSON.stringify({
        accessToken: "test-access-token",
        refreshToken: "rt_test",
        expiresAt: Date.now() + 3_600_000,
      })
    );
  });
}

test.describe("OrionaMesh MVP 浏览器主路径", () => {
  test("登录后可查看并更新本人资料", async ({ page }) => {
    const user = apiFixtures.user();
    await installApiMock(page, {
      "POST /v1/auth/sessions": () => ({ data: apiFixtures.session() }),
      "GET /v1/users/me": () => ({ data: user }),
      "PATCH /v1/users/me": () => ({ data: { ...user, display_name: "更新后的名称" } }),
      "GET /v1/knowledge-bases": () => ({ data: pageOf([]) }),
    });

    await page.goto("/login");
    await page.getByLabel("邮箱").fill(user.email);
    await page.getByLabel("密码").fill("safe-password");
    await page.getByRole("button", { name: "登录" }).click();
    await expect(page).toHaveURL(/\/knowledge-bases$/);

    await page.goto("/profile");
    const displayName = page.getByLabel("显示名");
    await expect(displayName).toHaveValue("Reader");
    await displayName.fill("更新后的名称");
    await page.getByRole("button", { name: "保存" }).click();
    await expect(displayName).toHaveValue("更新后的名称");
  });

  test("删除失败墓碑不暴露资料名，仅允许确认后重试删除", async ({ page }) => {
    const user = apiFixtures.user();
    const tombstone = apiFixtures.document({
      filename: "不得展示.md",
      status: "failed",
      current_task_type: "delete_cleanup",
      error_code: 20015,
      error_message: "资料删除未完成，请重试删除",
      allowed_actions: ["retry_delete"],
    });
    let deleteCount = 0;
    await restoreSession(page);
    await installApiMock(page, {
      "GET /v1/users/me": () => ({ data: user }),
      [`GET /v1/knowledge-bases/${tombstone.knowledge_base_id}/documents`]: () => ({
        data: pageOf([tombstone]),
      }),
      [`DELETE /v1/knowledge-bases/${tombstone.knowledge_base_id}/documents/${tombstone.id}`]:
        () => {
          deleteCount += 1;
          return { data: null };
        },
    });

    const meResponse = page.waitForResponse(
      (response) => response.url().endsWith("/v1/users/me") && response.status() === 200
    );
    await page.goto(`/knowledge-bases/${tombstone.knowledge_base_id}`);
    await meResponse;
    await expect(page.getByText("删除未完成", { exact: true })).toBeVisible();
    await expect(page.getByText("不得展示.md")).toHaveCount(0);
    await page.getByRole("button", { name: "重试删除" }).click();
    await expect(page.getByRole("alertdialog")).toBeVisible();
    expect(deleteCount).toBe(0);
    await page.getByRole("button", { name: "确认重试删除" }).click();
    await expect.poll(() => deleteCount).toBe(1);
  });

  test("上传携带幂等键、限流提示可见，资料状态轮询至终态", async ({ page }) => {
    const user = apiFixtures.user();
    const knowledgeBaseId = apiFixtures.knowledgeBase().id;
    const pending = apiFixtures.document({ status: "processing", filename: "轮询中.md" });
    const completed = { ...pending, status: "completed" as const };
    let listRequests = 0;
    let uploadRequests = 0;
    const keys: string[] = [];
    await restoreSession(page);
    await installApiMock(page, {
      "GET /v1/users/me": () => ({ data: user }),
      [`GET /v1/knowledge-bases/${knowledgeBaseId}/documents`]: () => {
        listRequests += 1;
        return { data: pageOf([listRequests > 1 ? completed : pending]) };
      },
      [`POST /v1/knowledge-bases/${knowledgeBaseId}/documents`]: (route) => {
        uploadRequests += 1;
        keys.push(route.request().headers()["idempotency-key"] ?? "");
        if (uploadRequests === 1) return { status: 202, data: { documents: [pending] } };
        return apiError(429, 10005, "请求过于频繁，请稍后再试", { "Retry-After": "3" });
      },
    });

    await page.goto(`/knowledge-bases/${knowledgeBaseId}`);
    await expect(page.getByText("处理中")).toBeVisible();
    await expect.poll(() => listRequests, { timeout: 5_000 }).toBeGreaterThan(1);
    await expect(page.getByText("已完成")).toBeVisible();

    const file = { name: "retry.md", mimeType: "text/markdown", buffer: Buffer.from("# Retry") };
    await page.locator("#upload-files").setInputFiles(file);
    await expect.poll(() => uploadRequests).toBe(1);
    await page.locator("#upload-files").setInputFiles(file);
    await expect(
      page.getByRole("alert").filter({ hasText: "请求过于频繁，请稍后再试" })
    ).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys.every((key) => /^[A-Za-z0-9._:-]{8,128}$/.test(key))).toBe(true);
  });

  test("空资料失败与知识库删除均不会因重复点击产生额外 DELETE", async ({ page }) => {
    const user = apiFixtures.user();
    const knowledgeBase = apiFixtures.knowledgeBase({ name: "待删除知识库" });
    const emptyDocument = apiFixtures.document({
      status: "failed",
      filename: "空资料.md",
      error_code: 20010,
      error_message: "资料没有可嵌入的有效文本",
    });
    let documentDeleteCount = 0;
    let knowledgeBaseDeleteCount = 0;
    await restoreSession(page);
    await installApiMock(page, {
      "GET /v1/users/me": () => ({ data: user }),
      "GET /v1/knowledge-bases": () => ({ data: pageOf([knowledgeBase]) }),
      [`GET /v1/knowledge-bases/${emptyDocument.knowledge_base_id}/documents`]: () => ({
        data: pageOf([emptyDocument]),
      }),
      [`DELETE /v1/knowledge-bases/${emptyDocument.knowledge_base_id}/documents/${emptyDocument.id}`]:
        async () => {
          documentDeleteCount += 1;
          await new Promise((resolve) => setTimeout(resolve, 100));
          return { data: null };
        },
      [`DELETE /v1/knowledge-bases/${knowledgeBase.id}`]: async () => {
        knowledgeBaseDeleteCount += 1;
        await new Promise((resolve) => setTimeout(resolve, 100));
        return { data: null };
      },
    });

    await page.goto(`/knowledge-bases/${emptyDocument.knowledge_base_id}`);
    await expect(page.getByText("资料没有可嵌入的有效文本")).toBeVisible();
    await page.getByRole("button", { name: "删除" }).click();
    await page.getByRole("button", { name: "确认删除" }).dblclick();
    await expect.poll(() => documentDeleteCount).toBe(1);

    await page.goto("/knowledge-bases");
    await page.getByRole("button", { name: "删除待删除知识库" }).click();
    await page.getByRole("button", { name: "确认删除" }).dblclick();
    await expect.poll(() => knowledgeBaseDeleteCount).toBe(1);
  });

  test("知识库与其他资源不可见分别保留服务端安全提示", async ({ page }) => {
    const user = apiFixtures.user();
    const knowledgeBaseId = "00000000-0000-4000-8000-000000000099";
    const documentId = "00000000-0000-4000-8000-000000000098";
    await restoreSession(page);
    await installApiMock(page, {
      "GET /v1/users/me": () => ({ data: user }),
      [`GET /v1/knowledge-bases/${knowledgeBaseId}/documents`]: () => ({
        status: 404,
        code: 20002,
        msg: "请求的知识库不存在",
        data: null,
      }),
      [`GET /v1/knowledge-bases/${knowledgeBaseId}/documents/${documentId}`]: () =>
        apiError(404, 20007, "请求的资源不存在"),
    });

    await page.goto(`/knowledge-bases/${knowledgeBaseId}`);
    await expect(page.getByText("请求的知识库不存在")).toBeVisible();
    await page.goto(`/knowledge-bases/${knowledgeBaseId}?document=${documentId}`);
    await expect(page.getByText("请求的资源不存在")).toBeVisible();
  });

  test("对话完成流使用服务端消息与引用快照，且不提供资料定位入口", async ({ page }) => {
    const user = apiFixtures.user();
    const knowledgeBase = apiFixtures.knowledgeBase();
    const conversation = apiFixtures.conversation({ knowledge_base_id: knowledgeBase.id });
    const assistant = {
      id: "00000000-0000-4000-8000-000000000052",
      conversation_id: conversation.id,
      role: "assistant" as const,
      content: "来自服务端的完整回答。",
      status: "completed" as const,
      rewritten_query: null,
      finish_reason: "stop" as const,
      created_at: "2026-08-19T00:00:02Z",
    };
    const snapshot = apiFixtures.citation({
      source_type: "snapshot",
      document_id: null,
      chunk_id: null,
      filename: "历史资料.pdf",
      content: "保存于回答时刻的证据。",
    });
    let messageRequests = 0;
    await restoreSession(page);
    await installApiMock(page, {
      "GET /v1/users/me": () => ({ data: user }),
      "GET /v1/knowledge-bases": () => ({ data: pageOf([knowledgeBase]) }),
      "GET /v1/conversations": () => ({ data: pageOf([conversation]) }),
      [`GET /v1/conversations/${conversation.id}`]: () => ({ data: conversation }),
      [`GET /v1/conversations/${conversation.id}/messages`]: () => {
        messageRequests += 1;
        return {
          data: {
            items: messageRequests > 1 ? [assistant] : [],
            has_more: false,
            next_before: null,
          },
        };
      },
      [`POST /v1/conversations/${conversation.id}/messages`]: () =>
        sseResponse([
          sseFrame("message_start", { message_id: assistant.id }),
          sseFrame("retrieval_done", { citations: [snapshot] }),
          sseFrame("delta", { text: "来自服务端的完整回答。" }),
          sseFrame("message_end", { finish_reason: "stop" }),
        ]),
      [`GET /v1/conversations/${conversation.id}/messages/${assistant.id}/citations`]: () => ({
        data: pageOf([snapshot]),
      }),
    });

    await page.goto("/conversations");
    await page.selectOption("#conversation-knowledge-base", knowledgeBase.id);
    await page.getByRole("button", { name: "打开对话 测试对话" }).click();
    await expect(page).toHaveURL(new RegExp(`conversation=${conversation.id}`));
    await page.getByLabel("输入问题").fill("资料里写了什么？");
    await page.getByRole("button", { name: "发送" }).click();
    await expect(page.getByText("来自服务端的完整回答。")).toBeVisible();
    await page.getByRole("button", { name: "查看引用 历史资料.pdf" }).click();
    await expect(page.getByLabel("引用详情")).toContainText("来源快照");
    await expect(page.getByRole("link", { name: "定位到资料" })).toHaveCount(0);
  });

  test("SSE 错误与用户取消分别收敛为可理解终态", async ({ page }) => {
    const user = apiFixtures.user();
    const knowledgeBase = apiFixtures.knowledgeBase();
    const conversation = apiFixtures.conversation({ knowledge_base_id: knowledgeBase.id });
    let requestNumber = 0;
    const cancelledRequestControl: { release: (() => void) | null } = { release: null };
    const cancelledRequest = new Promise<void>((resolve) => {
      cancelledRequestControl.release = resolve;
    });
    await restoreSession(page);
    await installApiMock(page, {
      "GET /v1/users/me": () => ({ data: user }),
      "GET /v1/knowledge-bases": () => ({ data: pageOf([knowledgeBase]) }),
      "GET /v1/conversations": () => ({ data: pageOf([conversation]) }),
      [`GET /v1/conversations/${conversation.id}`]: () => ({ data: conversation }),
      [`GET /v1/conversations/${conversation.id}/messages`]: () => ({
        data: { items: [], has_more: false, next_before: null },
      }),
      [`POST /v1/conversations/${conversation.id}/messages`]: async () => {
        requestNumber += 1;
        if (requestNumber === 1) {
          return sseResponse([
            sseFrame("message_start", { message_id: "00000000-0000-4000-8000-000000000053" }),
            sseErrorFrame("error", 50000, "回答生成失败，请稍后重试。"),
          ]);
        }
        await cancelledRequest;
        return sseResponse([]);
      },
    });

    try {
      await page.goto("/conversations");
      await page.selectOption("#conversation-knowledge-base", knowledgeBase.id);
      await page.getByRole("button", { name: "打开对话 测试对话" }).click();
      await page.getByLabel("输入问题").fill("第一次问题");
      await page.getByRole("button", { name: "发送" }).click();
      await expect(
        page.getByRole("alert").filter({ hasText: "回答生成失败，请稍后重试。" })
      ).toBeVisible();

      await page.getByLabel("输入问题").fill("第二次问题");
      await page.getByRole("button", { name: "发送" }).click();
      await page.getByRole("button", { name: "取消" }).click();
      await expect(page.getByRole("status")).toContainText("回答已取消");
    } finally {
      cancelledRequestControl.release?.();
    }
  });
});
