"use client";

import Link from "next/link";

import { RequireAuth } from "@/features/auth/RequireAuth";
import { SignOutButton } from "@/features/auth/SignOutButton";
import { KnowledgeBaseList } from "@/features/knowledge-bases/KnowledgeBaseList";

export default function KnowledgeBasesPage() {
  return (
    <RequireAuth>
      <main className="mx-auto max-w-4xl space-y-6 p-4">
        <header className="flex items-center justify-between">
          <h1 className="text-xl font-semibold">知识库</h1>
          <nav className="flex items-center gap-4">
            <Link href="/profile" className="text-sm hover:underline">
              个人资料
            </Link>
            <SignOutButton />
          </nav>
        </header>
        <KnowledgeBaseList />
      </main>
    </RequireAuth>
  );
}
