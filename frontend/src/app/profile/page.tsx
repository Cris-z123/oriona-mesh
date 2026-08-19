"use client";

import { AppShell } from "@/components/app-shell/AppShell";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { ProfileForm } from "@/features/profile/ProfileForm";

export default function ProfilePage() {
  return (
    <RequireAuth>
      <AppShell>
        <div className="space-y-6">
          <header>
            <h1 className="font-display text-2xl font-semibold">个人资料</h1>
            <p className="text-sm text-muted-foreground">查看与更新本人基本资料</p>
          </header>
          <ProfileForm />
        </div>
      </AppShell>
    </RequireAuth>
  );
}
