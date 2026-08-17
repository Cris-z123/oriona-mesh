import "server-only";

import { pino, type Logger } from "pino";

/**
 * Next.js 服务端专用日志（T095）。
 *
 * - `import "server-only"` 保证本模块永远不会被打进客户端 bundle；
 * - 日志只用于服务端诊断（API 客户端记录调用元数据），并过滤敏感字段：
 *   token/密码/secret/API Key、请求头，以及资料内容与引用快照等业务 payload；
 * - 过滤是正则路径级 redact（pino redact 路径支持通配符），命中一律替换为
 *   `[redacted]`，与后端日志白名单契约（quickstart）保持一致。
 */
const REDACT_PATHS = [
  // 凭证与令牌
  "*.password",
  "*.passwd",
  "*.token",
  "*.access_token",
  "*.refresh_token",
  "*.secret",
  "*.secret_key",
  "*.api_key",
  "*.apiKey",
  "*.authorization",
  "req.headers",
  "headers.authorization",
  // 业务 payload：资料内容、引用快照
  "*.document_content",
  "*.content",
  "*.chunk",
  "*.preview",
  "*.snapshot",
  "*.citation",
  "*.question",
  "*.answer",
];

const CENSOR = "[redacted]";

export const serverLogger: Logger = pino({
  level: process.env.LOG_LEVEL ?? "info",
  redact: {
    paths: REDACT_PATHS,
    censor: CENSOR,
  },
  base: undefined,
  messageKey: "msg",
});
