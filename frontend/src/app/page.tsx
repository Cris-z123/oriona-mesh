import { redirect } from "next/navigation";

/** 首页：入口统一导向知识库列表。 */
export default function HomePage() {
  redirect("/knowledge-bases");
}
