import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

/**
 * OrionaMesh 前端构建配置。
 *
 * - `output: "standalone"`：供 `deploy/docker/frontend.Dockerfile`（T098）复制最小
 *   运行时（.next/standalone）使用；本地开发不受影响。
 * - 浏览器始终通过同源 `/v1` 访问契约；仅 pnpm dev 代理到本机 API，生产保持 Nginx 同源转发。
 */
export default function nextConfig(phase: string): NextConfig {
  const developmentApiUpstream = process.env.ORIONAMESH_API_DEV_UPSTREAM ?? "http://127.0.0.1:8000";

  return {
    output: "standalone",
    async rewrites() {
      if (phase !== PHASE_DEVELOPMENT_SERVER) return [];
      return [
        {
          source: "/v1/:path*",
          destination: `${developmentApiUpstream}/v1/:path*`,
        },
      ];
    },
  };
}
