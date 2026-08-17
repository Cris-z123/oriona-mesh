import type { NextConfig } from "next";

/**
 * OrionaMesh 前端构建配置。
 *
 * - `output: "standalone"`：供 `deploy/docker/frontend.Dockerfile`（T098）复制最小
 *   运行时（.next/standalone）使用；本地开发不受影响。
 * - 不在此处配置后端地址：前端只通过 `NEXT_PUBLIC_API_BASE_URL` 访问 `/v1` 契约。
 */
const nextConfig: NextConfig = {
  output: "standalone",
};

export default nextConfig;
