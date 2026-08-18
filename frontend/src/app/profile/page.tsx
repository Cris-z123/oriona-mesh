"use client";

import Link from "next/link";

import { RequireAuth } from "@/features/auth/RequireAuth";
import { SignOutButton } from "@/features/auth/SignOutButton";
import { ProfileForm } from "@/features/profile/ProfileForm";

export default function ProfilePage() {
  return (
    <RequireAuth>
      <main className="mx-auto max-w-4xl space-y-6 p-4">
        <header className="flex items-center justify-between">
          <h1 className="text-xl font-semibold">个人资料</h1>
          <nav className="flex items-center gap-4">
            <Link href="/knowledge-bases" className="text-sm hover:underline">
              知识库
            </Link>
            <SignOutButton />
          </nav>
        </header>
        <ProfileForm />
      </main>
    </RequireAuth>
  );
}
