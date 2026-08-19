import Link from "next/link";
import type { Metadata } from "next";

import { LoginForm } from "@/features/auth/LoginForm";

export const metadata: Metadata = { title: "登录 | OrionaMesh" };

export default function LoginPage() {
  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm space-y-6">
        <div>
          <h1 className="text-xl font-semibold">登录</h1>
          <p className="text-sm text-muted-foreground">登录后管理你的私有知识库</p>
        </div>
        <LoginForm />
        <p className="text-center text-sm text-muted-foreground">
          还没有账号？{" "}
          <Link href="/register" className="text-primary hover:underline">
            注册
          </Link>
        </p>
      </div>
    </main>
  );
}
