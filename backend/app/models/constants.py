"""模型层共享常量。

异步处理失败的稳定业务错误码集合（openapi.yaml ErrorCode 中
20001/20010~20015/50000 段）；资料与任务两表的 CheckConstraint 共用，
保证持久化错误码口径一致。
"""

# 20011 在 20010 与 20012 之间，故单独列出以保持数值语义清晰。
ASYNC_ERROR_CODES: tuple[int, ...] = (20001, 20010, 20011, 20012, 20013, 20014, 20015, 50000)

# 删除清理失败稳定错误码；知识库 delete_failed 状态与资料 delete_cleanup 失败共用。
DELETE_CLEANUP_ERROR_CODE = 20015
