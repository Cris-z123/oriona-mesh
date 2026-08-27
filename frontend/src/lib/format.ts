/**
 * 面向用户的可读格式（FR-005）：文件大小与时间统一在此转换，
 * 不得在组件中直接暴露原始字节数或 ISO 时间字符串。
 */

/** 1024 进制可读文件大小；小于 1 KB 时显示字节。 */
export function formatFileSize(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} 字节`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

/** 本地化日期时间；无效或缺失输入显示占位符。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 面向用户的时长；小于 1 秒显示毫秒，其余按秒/分展示。 */
export function formatDuration(value: number | null | undefined): string {
  if (value == null || value < 0) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  const seconds = value / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds >= 10 ? 0 : 1)} 秒`;
  const minutes = Math.floor(seconds / 60);
  const restSeconds = Math.round(seconds % 60);
  return restSeconds === 0 ? `${minutes} 分` : `${minutes} 分 ${restSeconds} 秒`;
}
