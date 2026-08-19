import { Button } from "@/components/ui/button";

/** 删除当前页最后一项时返回应展示的页码，避免列表落在空的末页。 */
export function pageAfterDeletingLastItem(page: number, itemCount: number): number {
  return itemCount === 1 && page > 1 ? page - 1 : page;
}

/** 统一页码控制，资源列表只提供页码、总页数与加载状态。 */
export function Pagination({
  page,
  pageCount,
  isFetching = false,
  onPageChange,
  summary,
}: {
  page: number;
  pageCount: number;
  isFetching?: boolean;
  onPageChange: (page: number) => void;
  summary?: string;
}) {
  return (
    <nav className="flex items-center gap-2 text-sm" aria-label="分页">
      <Button
        variant="outline"
        disabled={page <= 1 || isFetching}
        onClick={() => onPageChange(page - 1)}
      >
        上一页
      </Button>
      <span>
        第 {page} / {pageCount} 页{summary ? `，${summary}` : ""}
      </span>
      <Button
        variant="outline"
        disabled={page >= pageCount || isFetching}
        onClick={() => onPageChange(page + 1)}
      >
        下一页
      </Button>
    </nav>
  );
}
