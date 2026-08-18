"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/** 首页：入口统一导向知识库列表。 */
export default function HomePage() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/knowledge-bases");
  }, [router]);

  return null;
}
