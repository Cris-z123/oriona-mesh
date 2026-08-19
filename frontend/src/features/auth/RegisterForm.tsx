"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { asApiError, register } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** 注册表单（FR-001）：本地结构校验由 RHF + Zod resolver 执行，服务端为最终执行者。 */
const registerSchema = z.object({
  email: z.string().trim().email("请输入有效邮箱地址"),
  password: z.string().min(8, "密码至少需要 8 个字符"),
  displayName: z.string().trim().max(100, "昵称不能超过 100 个字符"),
});

type RegisterValues = z.infer<typeof registerSchema>;

export function RegisterForm() {
  const router = useRouter();
  const {
    register: registerField,
    handleSubmit,
    formState: { errors, isSubmitting },
    setError,
  } = useForm<RegisterValues>({
    resolver: zodResolver(registerSchema),
    defaultValues: { email: "", password: "", displayName: "" },
  });

  const onSubmit = async (values: RegisterValues) => {
    try {
      await register({
        email: values.email,
        password: values.password,
        ...(values.displayName !== "" ? { display_name: values.displayName } : {}),
      });
      router.push("/login");
    } catch (err) {
      setError("root", { message: asApiError(err).msg });
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
          aria-describedby={errors.password ? "register-password-error" : undefined}
          {...registerField("password")}
        />
        {errors.password ? (
          <p id="register-password-error" role="alert" className="text-sm text-destructive">
            {errors.password.message}
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
      {errors.root ? (
        <p role="alert" className="text-sm text-destructive">
          {errors.root.message}
        </p>
      ) : null}
      <Button type="submit" disabled={isSubmitting} className="w-full">
        注册
      </Button>
    </form>
  );
}
