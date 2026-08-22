"use client";

import { Badge } from "@/components/ui/badge";
import { ErrorState } from "@/components/ui/error-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useDocumentTasks } from "@/features/documents/queries";
import type {
  DocumentTask,
  DocumentTaskAttemptStatus,
  DocumentTaskStatus,
  DocumentTaskType,
} from "@/lib/api/types";

/** 处理阶段中文标签（FR-005）：详情与任务历史共用，不得各自复制内部枚举文案。 */
export const taskTypeLabel: Record<DocumentTaskType, string> = {
  parse: "解析",
  chunk: "分块",
  embed: "嵌入",
  finalize: "最终校验",
  cleanup: "清理",
  delete_cleanup: "删除清理",
};

const taskStatusLabel: Record<DocumentTaskStatus, string> = {
  pending: "待处理",
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const attemptStatusLabel: Record<DocumentTaskAttemptStatus, string> = {
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

/** 处理阶段与每次 attempt 的服务端诊断记录；不推导重试或状态转换。 */
export function TaskHistory({
  knowledgeBaseId,
  documentId,
  tasks: providedTasks,
}: {
  knowledgeBaseId?: string;
  documentId?: string;
  tasks?: readonly DocumentTask[];
}) {
  if (providedTasks) return <TaskHistoryContent tasks={providedTasks} />;
  if (!knowledgeBaseId || !documentId) return null;
  return <TaskHistoryQuery knowledgeBaseId={knowledgeBaseId} documentId={documentId} />;
}

function TaskHistoryQuery({
  knowledgeBaseId,
  documentId,
}: {
  knowledgeBaseId: string;
  documentId: string;
}) {
  const taskQuery = useDocumentTasks(knowledgeBaseId, documentId);

  if (taskQuery.error) return <ErrorState error={taskQuery.error} />;
  if (!taskQuery.data) return <Skeleton className="h-28 w-full" aria-label="加载处理记录" />;
  return <TaskHistoryContent tasks={taskQuery.data.items} />;
}

function TaskHistoryContent({ tasks }: { tasks: readonly DocumentTask[] }) {
  if (tasks.length === 0) return <p className="text-sm text-muted-foreground">暂无处理记录</p>;

  return (
    <section aria-label="处理记录" className="space-y-3 border-t pt-3">
      <h4 className="font-medium">处理记录</h4>
      <ol className="space-y-3">
        {tasks.map((task) => (
          <li key={task.id} className="rounded-md border p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{taskTypeLabel[task.task_type]}</span>
              <Badge variant={task.status === "failed" ? "destructive" : "secondary"}>
                {taskStatusLabel[task.status]}
              </Badge>
            </div>
            <div className="mt-2 grid gap-1 text-muted-foreground sm:grid-cols-2">
              <span>版本 {task.document_version}</span>
              <span>
                重试 {task.retry_count} / {task.max_retries}
              </span>
              <span>删除轮次 {task.delete_cycle}</span>
              <span>
                {task.total_items === null
                  ? `已处理 ${task.processed_items} 项`
                  : `${task.processed_items} / ${task.total_items}`}
              </span>
              {task.queued_at ? <span>排队：{task.queued_at}</span> : null}
              {task.started_at ? <span>开始：{task.started_at}</span> : null}
              {task.finished_at ? <span>结束：{task.finished_at}</span> : null}
            </div>
            {task.error_code !== null && task.error_message ? (
              <p className="mt-2 text-destructive">
                <span>错误码 {task.error_code}</span>：<span>{task.error_message}</span>
              </p>
            ) : null}
            {task.attempts.length > 0 ? (
              <ol
                className="mt-3 space-y-2 border-l pl-3"
                aria-label={`${taskTypeLabel[task.task_type]} 尝试记录`}
              >
                {task.attempts.map((attempt) => (
                  <li key={attempt.id}>
                    <p>
                      尝试 {attempt.attempt_no} · {attemptStatusLabel[attempt.status]} ·{" "}
                      {attempt.worker_name ?? "未标识 worker"}
                      {attempt.duration_ms === null ? "" : ` · ${attempt.duration_ms} ms`}
                    </p>
                    <p className="text-muted-foreground">开始：{attempt.started_at}</p>
                    {attempt.finished_at ? (
                      <p className="text-muted-foreground">结束：{attempt.finished_at}</p>
                    ) : null}
                    {attempt.error_message ? (
                      <p className="text-destructive">{attempt.error_message}</p>
                    ) : null}
                  </li>
                ))}
              </ol>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  );
}
