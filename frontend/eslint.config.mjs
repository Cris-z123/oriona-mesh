import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import prettier from "eslint-config-prettier";

/**
 * ESLint 扁平配置（ESLint 10 + eslint-config-next 16）。
 * - 规则基于 Next.js core-web-vitals 与 TypeScript 预设；
 * - 样式问题统一交给 Prettier（`pnpm format:check` / `pnpm format`），ESLint 不重复。
 */
export default defineConfig([
  ...nextVitals,
  ...nextTs,
  prettier,
  globalIgnores([".next/**", "out/**", "build/**", "coverage/**", "next-env.d.ts"]),
]);
