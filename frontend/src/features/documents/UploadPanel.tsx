"use client";

import { useCallback, useRef, useState, type ChangeEvent, type DragEvent } from "react";

import { ErrorState } from "@/components/ui/error-state";
import { Label } from "@/components/ui/label";
import { ApiError, asApiError, generateIdempotencyKey, uploadDocuments } from "@/lib/api/client";
import { UPLOAD_LIMITS, type Document } from "@/lib/api/types";
import { cn } from "@/lib/utils";

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
  onUploaded?: (documents: Document[]) => void;
}) {
  const [hint, setHint] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [acceptedCount, setAcceptedCount] = useState<number | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFiles = useCallback(
    async (files: File[]) => {
      // 上传中忽略新选择/拖放（T154）：本批完成前不并发启动下一批。
      if (files.length === 0 || uploading) return;
      setHint(null);
      setError(null);
      // 新一轮上传或失败后清空上一次的“已接收”反馈，避免误导为仍在处理。
      setAcceptedCount(null);
      const invalid = validateFiles(files);
      if (invalid) {
        setHint(invalid);
        return;
      }
      setUploading(true);
      setProgress(0);
      try {
        // 每次上传请求使用新生成的请求级幂等键（FR-031）
        const result = await uploadDocuments(knowledgeBaseId, files, {
          idempotencyKey: generateIdempotencyKey(),
          onProgress: (loaded, total) => {
            if (total > 0) setProgress(Math.round((loaded / total) * 100));
          },
        });
        setProgress(null);
        setAcceptedCount(result.documents.length);
        onUploaded?.(result.documents);
      } catch (err) {
        setAcceptedCount(null);
        setError(asApiError(err));
      } finally {
        setUploading(false);
        if (inputRef.current) inputRef.current.value = "";
      }
    },
    [knowledgeBaseId, onUploaded, uploading]
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
        <p className="text-muted-foreground">
          {uploading ? "正在上传，请等待本批完成…" : "拖放文件到此处，或"}
        </p>
        <Label
          htmlFor="upload-files"
          className={cn(
            "mt-1 inline-block text-primary",
            uploading ? "cursor-not-allowed opacity-50" : "cursor-pointer"
          )}
        >
          选择文件
        </Label>
        <input
          id="upload-files"
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.docx,.md,.txt"
          className="sr-only"
          disabled={uploading}
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
          正在上传… {progress}%
        </p>
      ) : null}
      {hint ? (
        <p role="alert" className="text-sm text-destructive">
          {hint}
        </p>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
      {acceptedCount !== null ? (
        <p aria-live="polite" className="text-sm text-primary">
          已接收 {acceptedCount} 份资料，正在处理
        </p>
      ) : null}
      <p className="text-xs text-muted-foreground">
        单文件最大 50MB，单次最多 20 个；仅支持 PDF、DOCX、MD 和 TXT。
      </p>
    </div>
  );
}
