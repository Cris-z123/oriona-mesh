"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { asApiError, updateMe } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/features/auth/AuthProvider";

/** 本人基本资料表单（FR-002）：本地结构校验由 RHF + Zod resolver 执行，服务端为最终执行者。 */
const profileSchema = z.object({
  displayName: z.string().trim().min(1, "请输入显示名").max(100, "显示名不能超过 100 个字符"),
});

type ProfileValues = z.infer<typeof profileSchema>;

/** 表单内部组件：以 user.id 为 key 挂载，保证显示名状态与当前用户一致。 */
function ProfileFormInner() {
  const { user, updateCurrentUser } = useAuth();
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
    setError,
    reset,
  } = useForm<ProfileValues>({
    resolver: zodResolver(profileSchema),
    defaultValues: { displayName: user?.display_name ?? "" },
  });

  if (!user) return null;

  const onSubmit = async (values: ProfileValues) => {
    try {
      const updated = await updateMe({ display_name: values.displayName });
      reset({ displayName: updated.display_name ?? "" });
      updateCurrentUser(updated);
    } catch (err) {
      setError("root", { message: asApiError(err).msg });
    }
  };

  return (
    <form noValidate onSubmit={handleSubmit(onSubmit)} className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="profile-display-name">显示名</Label>
        <Input
          id="profile-display-name"
          aria-invalid={errors.displayName ? true : undefined}
          aria-describedby={errors.displayName ? "profile-display-name-error" : undefined}
          {...register("displayName")}
        />
        {errors.displayName ? (
          <p id="profile-display-name-error" role="alert" className="text-sm text-destructive">
            {errors.displayName.message}
          </p>
        ) : null}
      </div>
      <p className="text-sm text-muted-foreground">邮箱：{user.email}</p>
      {errors.root ? (
        <p role="alert" className="text-sm text-destructive">
          {errors.root.message}
        </p>
      ) : null}
      <Button type="submit" disabled={isSubmitting}>
        保存
      </Button>
    </form>
  );
}

/** 本人基本资料表单（FR-002）：用户加载完成后以 user.id 为 key 挂载表单。 */
export function ProfileForm() {
  const { user, ready } = useAuth();
  if (!ready || !user) return null;
  return <ProfileFormInner key={user.id} />;
}
