import Link from "next/link";
import type { Metadata } from "next";
import { Orbit } from "lucide-react";

import { LoginForm } from "@/features/auth/LoginForm";

export const metadata: Metadata = { title: "登录 | OrionaMesh" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ registered?: string }>;
}) {
  const { registered } = await searchParams;

  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-5 sm:p-8">
      <div className="w-full max-w-md">
        <Link
          href="/"
          className="mb-10 inline-flex items-center gap-2 rounded-md text-sm font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <span className="grid h-8 w-8 place-items-center rounded-md border border-primary/30 bg-primary/5 text-primary">
            <Orbit className="h-4 w-4" aria-hidden />
          </span>
          <span>OrionaMesh</span>
        </Link>
        <div className="border border-border bg-surface p-6 shadow-sm sm:p-8">
          <div className="mb-7 space-y-2">
            <p className="text-xs font-medium tracking-[0.18em] text-primary">WORKSPACE ACCESS</p>
            <h1 className="font-display text-3xl font-semibold tracking-tight">登录</h1>
            <p className="text-sm leading-6 text-muted-foreground">进入你的私有知识工作区。</p>
          </div>
          <LoginForm registered={registered === "1"} />
          <p className="mt-6 border-t pt-5 text-center text-sm text-muted-foreground">
            还没有账号？{" "}
            <Link href="/register" className="font-medium text-primary hover:underline">
              创建账号
            </Link>
          </p>
        </div>
      </div>
    </main>
  );
}
