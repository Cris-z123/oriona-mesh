import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DeleteDocumentDialog } from "@/features/documents/DeleteDocumentDialog";
import { TaskHistory } from "@/features/documents/TaskHistory";
import type { Document, DocumentTask } from "@/lib/api/types";

/**
 * T118 [P] [US3] 资料处理诊断与删除组件测试。
 *
 * 契约来源：openapi 的 DocumentTask/DocumentTaskAttempt，以及 FR-007、FR-008、
 * FR-008a、FR-010～FR-012。前端只能呈现服务端 DTO，不能自行增加重处理或替换入口。
 */
const FAILED_DOCUMENT = {
  id: "d-failed",
  knowledge_base_id: "kb-1",
  filename: "broken.pdf",
  file_type: "pdf",
  file_size: 128,
  status: "failed",
  version: 1,
  current_task_type: "finalize",
  retry_count: 3,
  delete_cycle: 0,
  chunk_count: 24,
  error_code: 20013,
  error_message: "资料处理结果不一致，请删除后重新上传",
  processing_started_at: "2026-08-18T00:00:00Z",
  processing_finished_at: "2026-08-18T00:02:00Z",
  created_at: "2026-08-18T00:00:00Z",
  updated_at: "2026-08-18T00:02:00Z",
  allowed_actions: ["delete"],
} satisfies Document;

const FAILED_TASK = {
  id: "task-finalize",
  document_id: FAILED_DOCUMENT.id,
  document_version: 1,
  task_type: "finalize",
  delete_cycle: 0,
  status: "failed",
  retry_count: 3,
  max_retries: 3,
  total_items: 40,
  processed_items: 24,
  error_code: 20013,
  error_message: "资料处理结果不一致，请删除后重新上传",
  queued_at: "2026-08-18T00:00:00Z",
  started_at: "2026-08-18T00:00:01Z",
  finished_at: "2026-08-18T00:02:00Z",
  created_at: "2026-08-18T00:00:00Z",
  updated_at: "2026-08-18T00:02:00Z",
  attempts: [
    {
      id: "attempt-1",
      task_id: "task-finalize",
      attempt_no: 1,
      worker_name: "worker-a",
      status: "failed",
      started_at: "2026-08-18T00:00:01Z",
      finished_at: "2026-08-18T00:00:30Z",
      error_message: "资料处理结果不一致，请删除后重新上传",
      duration_ms: 29000,
      created_at: "2026-08-18T00:00:01Z",
    },
    {
      id: "attempt-4",
      task_id: "task-finalize",
      attempt_no: 4,
      worker_name: "worker-b",
      status: "failed",
      started_at: "2026-08-18T00:01:30Z",
      finished_at: "2026-08-18T00:02:00Z",
      error_message: "资料处理结果不一致，请删除后重新上传",
      duration_ms: 30000,
      created_at: "2026-08-18T00:01:30Z",
    },
  ],
} satisfies DocumentTask;

const DELETE_TOMBSTONE = {
  ...FAILED_DOCUMENT,
  id: "d-delete-failed",
  filename: "must-not-be-shown.pdf",
  current_task_type: "delete_cleanup",
  delete_cycle: 2,
  error_code: 20015,
  error_message: "资料删除未完成，请重试删除",
  allowed_actions: ["retry_delete"],
} satisfies Document;

describe("US3 任务诊断与资料删除", () => {
  it("完整呈现失败终态任务的阶段、进度、持久化错误和每次尝试", () => {
    render(<TaskHistory tasks={[FAILED_TASK]} />);

    expect(screen.getByText("最终校验")).toBeInTheDocument();
    expect(screen.queryByText("finalize")).not.toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(screen.getByText("24 / 40")).toBeInTheDocument();
    expect(screen.getByText("版本 1")).toBeInTheDocument();
    expect(screen.getByText("重试 3 / 3")).toBeInTheDocument();
    expect(screen.getByText("删除轮次 0")).toBeInTheDocument();
    expect(screen.getByText("错误码 20013")).toBeInTheDocument();
    expect(screen.getAllByText("资料处理结果不一致，请删除后重新上传")).toHaveLength(3);
    expect(screen.getByText(/尝试 1/)).toBeInTheDocument();
    expect(screen.getByText(/尝试 4/)).toBeInTheDocument();
    expect(screen.getByText(/worker-a/)).toBeInTheDocument();
    expect(screen.getByText(/29000 ms/)).toBeInTheDocument();
    expect(screen.getByText(/worker-b/)).toBeInTheDocument();
    expect(screen.getByText(/30000 ms/)).toBeInTheDocument();

    expect(
      screen.queryByRole("button", { name: /重新处理|重试处理|替换/ })
    ).not.toBeInTheDocument();
  });

  it("普通失败资料须经危险删除确认后才提交删除，且没有重处理或替换入口", () => {
    const onDelete = vi.fn();
    render(<DeleteDocumentDialog document={FAILED_DOCUMENT} onDelete={onDelete} />);

    fireEvent.click(screen.getByRole("button", { name: /^删除$/ }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /确认删除/ }));
    expect(onDelete).toHaveBeenCalledWith("delete");
    expect(
      screen.queryByRole("button", { name: /重新处理|重试处理|替换/ })
    ).not.toBeInTheDocument();
  });

  it("20015 仅展示最小删除未完成墓碑，并经确认触发 retry_delete", () => {
    const onDelete = vi.fn();
    render(<DeleteDocumentDialog document={DELETE_TOMBSTONE} onDelete={onDelete} />);

    expect(screen.getByText("删除未完成")).toBeInTheDocument();
    expect(screen.getByText("资料删除未完成，请重试删除")).toBeInTheDocument();
    expect(screen.queryByText("must-not-be-shown.pdf")).not.toBeInTheDocument();
    expect(screen.getByText("错误码 20015")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /重试删除/ }));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /确认重试删除/ }));
    expect(onDelete).toHaveBeenCalledWith("retry_delete");
    expect(
      screen.queryByRole("button", { name: /重新处理|重试处理|替换/ })
    ).not.toBeInTheDocument();
  });
});
