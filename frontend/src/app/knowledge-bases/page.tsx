import type { Metadata } from "next";
import { AppShell } from "@/components/app-shell/AppShell";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { KnowledgeBaseList } from "@/features/knowledge-bases/KnowledgeBaseList";

export const metadata: Metadata = { title: "知识库 | OrionaMesh" };

export default function KnowledgeBasesPage() {
  return (
    <RequireAuth>
      <AppShell>
        <KnowledgeBaseList />
      </AppShell>
    </RequireAuth>
  );
}
