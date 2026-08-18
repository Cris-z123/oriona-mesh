"use client";

import { useState, type FormEvent } from "react";

import { ApiError, updateMe } from "@/lib/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/features/auth/AuthProvider";

/** 表单内部组件：以 user.id 为 key 挂载，保证显示名状态与当前用户一致。 */
function ProfileFormInner() {
  const { user } = useAuth();
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  if (!user) return null;

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setSaving(true);
    try {
      const updated = await updateMe({ display_name: displayName });
      setDisplayName(updated.display_name ?? "");
    } catch (err) {
      setError(err instanceof ApiError ? err.msg : "系统繁忙，请稍后再试");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="profile-display-name">显示名</Label>
        <Input
          id="profile-display-name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
        />
      </div>
      <p className="text-sm text-muted-foreground">邮箱：{user.email}</p>
      {error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}
      <Button type="submit" disabled={saving}>
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
