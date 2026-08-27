# Specification Quality Checklist: 阶段 18 安全解析运行时兼容性

**Purpose**: 验证阶段 18 需求在进入实施前完整、可测试且保持既有安全解析与 Celery 运行边界。
**Created**: 2026-08-26
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] 阶段 18 仅修复安全解析运行时兼容性和 worker control healthcheck，不扩大 REST/SSE、授权、数据模型或模型出口范围。
- [x] 阶段 18 明确保留 Celery prefork、独立进程隔离、超时强制回收及既有 `20001/20010` 错误语义。
- [x] 受影响的用户故事、边界情况、功能需求、数据边界、研究决策、计划、任务和部署验收说明均已更新。
- [x] 用户需求、架构/实施、数据不变量与执行任务分别以 `spec.md`、`plan.md`、`data-model.md`、`tasks.md` 为唯一权威来源。

## Requirement Completeness

- [x] 无 `[NEEDS CLARIFICATION]` 标记。
- [x] daemon parent 成功解析、runner 超时/协议/未知异常、空文本和已知解析失败均具有确定性测试路径。
- [x] 目标节点、control timeout、Docker health timeout/start period 与完整 Compose `healthy` 验收均具有可验证要求。
- [x] 解析通信不传递 Python 对象或不受控的异常正文，日志不记录资料内容，保持既有数据最小化和错误码边界。

## Feature Readiness

- [x] 所有阶段 18 功能需求都有对应单元、交付静态或完整 Compose 验收项。
- [x] T182 完成文档对齐；T183/T184 先建立失败测试；T185/T186 实现、部署配置和验证收尾。
- [x] 不将本次运行时修复变成降低并发、取消子进程隔离或放宽解析时限的临时方案。

## Notes

- 已确认“独立子进程”是操作系统子进程与受限字节协议；不是在 Celery daemon 中嵌套 `multiprocessing`，也不是 `solo`/线程替代方案。
- 当前问题来自 Python daemon process 禁止创建 `multiprocessing.Process`；修复必须在默认正式 worker 配置下验证。
