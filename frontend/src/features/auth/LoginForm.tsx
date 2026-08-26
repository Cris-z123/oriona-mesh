"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { useState } from "react";
import { z } from "zod";

import { asApiError, login } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** 登录表单（FR-001）：本地结构校验由 RHF + Zod resolver 执行，服务端 code/msg 为最终反馈。 */
const loginSchema = z.object({
  email: z.string().trim().email("请输入有效邮箱地址"),
  password: z.string().min(1, "请输入密码"),
});

type LoginValues = z.infer<typeof loginSchema>;

export function LoginForm({ registered = false }: { registered?: boolean }) {
  const router = useRouter();
  const [apiError, setApiError] = useState<ReturnType<typeof asApiError> | null>(null);
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: "", password: "" },
  });

  const onSubmit = async (values: LoginValues) => {
    try {
      setApiError(null);
      await login(values);
      router.push("/knowledge-bases");
    } catch (err) {
      setApiError(asApiError(err));
    }
  };

  return (
    <form noValidate onSubmit={handleSubmit(onSubmit)} className="space-y-4">
      {registered ? (
        <p
          role="status"
          className="rounded-md border border-primary/25 bg-primary/5 px-3 py-2 text-sm text-foreground"
        >
          注册成功，请使用新账号登录。
        </p>
      ) : null}
      <div className="space-y-1.5">
        <Label htmlFor="login-email">邮箱</Label>
        <Input
          id="login-email"
          type="email"
          autoComplete="email"
          aria-invalid={errors.email ? true : undefined}
          aria-describedby={errors.email ? "login-email-error" : undefined}
          {...register("email")}
        />
        {errors.email ? (
          <p id="login-email-error" role="alert" className="text-sm text-destructive">
            {errors.email.message}
          </p>
        ) : null}
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="login-password">密码</Label>
        <Input
          id="login-password"
          type="password"
          autoComplete="current-password"
          aria-invalid={errors.password ? true : undefined}
          aria-describedby={errors.password ? "login-password-error" : undefined}
          {...register("password")}
        />
        {errors.password ? (
          <p id="login-password-error" role="alert" className="text-sm text-destructive">
            {errors.password.message}
          </p>
        ) : null}
      </div>
      {apiError ? <ErrorState error={apiError} /> : null}
      <Button type="submit" disabled={isSubmitting} className="w-full">
        登录
      </Button>
    </form>
  );
}
