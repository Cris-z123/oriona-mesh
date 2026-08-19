"use client";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";

/** 用于知识库、会话等不可逆操作的统一确认交互。 */
export function DangerousActionDialog({
  triggerLabel,
  triggerAriaLabel,
  title,
  description,
  confirmLabel = "确认删除",
  pending = false,
  onConfirm,
}: {
  triggerLabel: string;
  triggerAriaLabel?: string;
  title: string;
  description: string;
  confirmLabel?: string;
  pending?: boolean;
  onConfirm: () => void | Promise<void>;
}) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant="destructive" disabled={pending} aria-label={triggerAriaLabel}>
          {triggerLabel}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogTitle>{title}</AlertDialogTitle>
        <AlertDialogDescription>{description}</AlertDialogDescription>
        <div className="mt-5 flex justify-end gap-2">
          <AlertDialogCancel asChild>
            <Button variant="outline" disabled={pending}>
              取消
            </Button>
          </AlertDialogCancel>
          <AlertDialogAction asChild>
            <Button variant="destructive" disabled={pending} onClick={() => void onConfirm()}>
              {confirmLabel}
            </Button>
          </AlertDialogAction>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  );
}
