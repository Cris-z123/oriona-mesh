"use client";

import { useCallback, useRef, useState, type ChangeEvent, type DragEvent } from "react";

import { ApiErrorNotice } from "@/components/ApiErrorNotice";
import { Label } from "@/components/ui/label";
import { ApiError, asApiError, generateIdempotencyKey, uploadDocuments } from "@/lib/api/client";
import { UPLOAD_LIMITS } from "@/lib/api/types";

const SUPPORTED_EXTENSIONS = ["pdf", "docx", "md", "txt"];

const HINT_TOO_MANY = "单次上传最多 20 个文件";
const HINT_TOO_LARGE = "文件超过 50MB 限制";
const HINT_UNSUPPORTED = "仅支持 PDF、DOCX、MD 和 TXT 文件";

/**
 * 客户端前置提示（FR-024/025）：与 openapi 固定文案一致，但服务端仍是最终执行者；
 * 任何提示只阻止本次请求，不复制或绕过后端校验。
 */
function validateFiles(files: File[]): string | null {
  if (files.length > UPLOAD_LIMITS.maxFiles) return HINT_TOO_MANY;
  for (const file of files) {
    const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
    if (!SUPPORTED_EXTENSIONS.includes(ext)) return HINT_UNSUPPORTED;
    if (file.size > UPLOAD_LIMITS.maxFileBytes) return HINT_TOO_LARGE;
  }
  return null;
}

/**
 * 批量上传面板（T111/FR-004）：批量选择/拖放、50MB/20 文件提示、
 * 每次请求自动生成请求级 Idempotency-Key、xhr.upload 进度渲染。
 */
export function UploadPanel({
  knowledgeBaseId,
  onUploaded,
}: {
  knowledgeBaseId: string;
  onUploaded?: () => void;
}) {
  const [hint, setHint] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFiles = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;
      setHint(null);
      setError(null);
      const invalid = validateFiles(files);
      if (invalid) {
        setHint(invalid);
        return;
      }
      setUploading(true);
      setProgress(0);
      try {
        // 每次上传请求使用新生成的请求级幂等键（FR-031）
        await uploadDocuments(knowledgeBaseId, files, {
          idempotencyKey: generateIdempotencyKey(),
          onProgress: (loaded, total) => {
            if (total > 0) setProgress(Math.round((loaded / total) * 100));
          },
        });
        setProgress(null);
        onUploaded?.();
      } catch (err) {
        setError(asApiError(err));
      } finally {
        setUploading(false);
        if (inputRef.current) inputRef.current.value = "";
      }
    },
    [knowledgeBaseId, onUploaded]
  );

  const onChange = (event: ChangeEvent<HTMLInputElement>) => {
    void handleFiles(Array.from(event.target.files ?? []));
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    void handleFiles(Array.from(event.dataTransfer.files));
  };

  return (
    <div className="space-y-3">
      <div
        className={`rounded-md border border-dashed p-4 text-center text-sm transition-colors ${
          dragging ? "border-ring bg-accent" : "border-input"
        }`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <p className="text-muted-foreground">拖放文件到此处，或</p>
        <Label htmlFor="upload-files" className="mt-1 inline-block cursor-pointer text-primary">
          选择文件
        </Label>
        <input
          id="upload-files"
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.docx,.md,.txt"
          className="sr-only"
          onChange={onChange}
        />
      </div>

      {uploading && progress !== null ? (
        <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
          <div className="h-full bg-primary transition-all" style={{ width: `${progress}%` }} />
        </div>
      ) : null}
      {uploading && progress !== null ? (
        <p className="text-sm text-muted-foreground" aria-live="polite">
          {progress}%
        </p>
      ) : null}
      {hint ? (
        <p role="alert" className="text-sm text-destructive">
          {hint}
        </p>
      ) : null}
      {error ? <ApiErrorNotice error={error} /> : null}
      <p className="text-xs text-muted-foreground">
        单文件最大 50MB，单次最多 20 个；仅支持 PDF、DOCX、MD 和 TXT。
      </p>
    </div>
  );
}
