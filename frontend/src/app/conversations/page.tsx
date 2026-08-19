import type { Metadata } from "next";
import { Suspense } from "react";

import { ConversationsWorkspace } from "@/features/conversations/ConversationsWorkspace";

export const metadata: Metadata = { title: "对话 | OrionaMesh" };

export default function ConversationsPage() {
  return (
    <Suspense fallback={<div className="p-6 text-sm text-muted-foreground">正在加载对话…</div>}>
      <ConversationsWorkspace />
    </Suspense>
  );
}
