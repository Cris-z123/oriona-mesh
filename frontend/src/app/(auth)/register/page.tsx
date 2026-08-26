import Link from "next/link";
import type { Metadata } from "next";
import { Orbit } from "lucide-react";

import { RegisterForm } from "@/features/auth/RegisterForm";

export const metadata: Metadata = { title: "注册 | OrionaMesh" };

export default function RegisterPage() {
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
            <p className="text-xs font-medium tracking-[0.18em] text-primary">CREATE WORKSPACE</p>
            <h1 className="font-display text-3xl font-semibold tracking-tight">创建账号</h1>
            <p className="text-sm leading-6 text-muted-foreground">
              建立你的私有知识工作区，所有资料只对你可见。
            </p>
          </div>
          <RegisterForm />
          <p className="mt-6 border-t pt-5 text-center text-sm text-muted-foreground">
            已有账号？{" "}
            <Link href="/login" className="font-medium text-primary hover:underline">
              去登录
            </Link>
          </p>
        </div>
      </div>
    </main>
  );
}
