"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { useState } from "react";
import { z } from "zod";

import { asApiError, register } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ErrorState } from "@/components/ui/error-state";

/**
 * 注册表单（FR-001）：本地结构校验由 RHF + Zod resolver 执行，服务端为最终执行者。
 * 密码“至少 8 字符且同时含字母和数字”是服务端规则（backend/app/core/password_policy.py、
 * openapi.yaml RegisterInput.pattern）；此处仅作即时提示并保持同一规则，服务端拒绝仍兜底。
 */
const registerSchema = z
  .object({
    email: z.string().trim().email("请输入有效邮箱地址"),
    password: z.string().min(8, "密码至少需要 8 个字符"),
    confirmPassword: z.string(),
    displayName: z.string().trim().max(100, "昵称不能超过 100 个字符"),
  })
  .superRefine(({ password, confirmPassword }, ctx) => {
    if (password.length >= 8 && (!/[A-Za-z]/.test(password) || !/\d/.test(password))) {
      ctx.addIssue({ code: "custom", path: ["password"], message: "密码必须同时包含字母和数字" });
    }
    if (password !== confirmPassword) {
      ctx.addIssue({ code: "custom", path: ["confirmPassword"], message: "两次输入的密码不一致" });
    }
  });

type RegisterValues = z.infer<typeof registerSchema>;

export function RegisterForm() {
  const router = useRouter();
  const [apiError, setApiError] = useState<ReturnType<typeof asApiError> | null>(null);
  const {
    register: registerField,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<RegisterValues>({
    resolver: zodResolver(registerSchema),
    defaultValues: { email: "", password: "", confirmPassword: "", displayName: "" },
  });

  const onSubmit = async (values: RegisterValues) => {
    try {
      setApiError(null);
      await register({
        email: values.email,
        password: values.password,
        ...(values.displayName !== "" ? { display_name: values.displayName } : {}),
      });
      router.push("/login");
    } catch (err) {
      setApiError(asApiError(err));
    }
  };

  return (
    <form noValidate onSubmit={handleSubmit(onSubmit)} className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="register-email">邮箱</Label>
        <Input
          id="register-email"
          type="email"
          autoComplete="email"
          aria-invalid={errors.email ? true : undefined}
          aria-describedby={errors.email ? "register-email-error" : undefined}
          {...registerField("email")}
        />
        {errors.email ? (
          <p id="register-email-error" role="alert" className="text-sm text-destructive">
            {errors.email.message}
          </p>
        ) : null}
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="register-password">密码</Label>
        <Input
          id="register-password"
          type="password"
          autoComplete="new-password"
          aria-invalid={errors.password ? true : undefined}
          aria-describedby={[
            "register-password-hint",
            errors.password ? "register-password-error" : "",
          ]
            .filter(Boolean)
            .join(" ")}
          {...registerField("password")}
        />
        <p id="register-password-hint" className="text-xs text-muted-foreground">
          至少 8 个字符，同时包含字母和数字。
        </p>
        {errors.password ? (
          <p id="register-password-error" role="alert" className="text-sm text-destructive">
            {errors.password.message}
          </p>
        ) : null}
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="register-confirm-password">确认密码</Label>
        <Input
          id="register-confirm-password"
          type="password"
          autoComplete="new-password"
          aria-invalid={errors.confirmPassword ? true : undefined}
          aria-describedby={errors.confirmPassword ? "register-confirm-password-error" : undefined}
          {...registerField("confirmPassword")}
        />
        {errors.confirmPassword ? (
          <p id="register-confirm-password-error" role="alert" className="text-sm text-destructive">
            {errors.confirmPassword.message}
          </p>
        ) : null}
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="register-display-name">昵称</Label>
        <Input
          id="register-display-name"
          aria-invalid={errors.displayName ? true : undefined}
          aria-describedby={errors.displayName ? "register-display-name-error" : undefined}
          {...registerField("displayName")}
        />
        {errors.displayName ? (
          <p id="register-display-name-error" role="alert" className="text-sm text-destructive">
            {errors.displayName.message}
          </p>
        ) : null}
      </div>
      {apiError ? <ErrorState error={apiError} /> : null}
      <Button type="submit" disabled={isSubmitting} className="w-full">
        注册
      </Button>
    </form>
  );
}
