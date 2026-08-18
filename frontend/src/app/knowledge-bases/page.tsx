"use client";

import { AppShell } from "@/components/app-shell/AppShell";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { KnowledgeBaseList } from "@/features/knowledge-bases/KnowledgeBaseList";

export default function KnowledgeBasesPage() {
  return (
    <RequireAuth>
      <AppShell>
        <div className="space-y-6">
          <header>
            <h1 className="font-display text-2xl font-semibold">知识库</h1>
            <p className="text-sm text-muted-foreground">建立并维护私有知识库</p>
          </header>
          <KnowledgeBaseList />
        </div>
      </AppShell>
    </RequireAuth>
  );
}
