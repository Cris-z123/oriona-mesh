# OrionaMesh 开发指南

## 当前功能

- 当前实施范围：`specs/001-orionamesh-rag-mvp`。
- 开发前先阅读该目录的 [spec.md](./specs/001-orionamesh-rag-mvp/spec.md)、[plan.md](./specs/001-orionamesh-rag-mvp/plan.md) 和 [tasks.md](./specs/001-orionamesh-rag-mvp/tasks.md)。
- 项目不可协商原则以 [.specify/memory/constitution.md](./.specify/memory/constitution.md) 为准。

## 文档权威边界

不要在本文件复制业务规则或接口细节；发生冲突时按以下来源执行：

- 用户需求、范围和验收条件：[spec.md](./specs/001-orionamesh-rag-mvp/spec.md)
- 架构、模块边界和实施顺序：[plan.md](./specs/001-orionamesh-rag-mvp/plan.md)
- 数据不变量、状态机和事务：[data-model.md](./specs/001-orionamesh-rag-mvp/data-model.md)
- REST/SSE 与错误码：[openapi.yaml](./specs/001-orionamesh-rag-mvp/contracts/openapi.yaml)
- 模型出口、脱敏与审计：[model-egress.md](./specs/001-orionamesh-rag-mvp/contracts/model-egress.md)
- 配置、部署和验证：[quickstart.md](./specs/001-orionamesh-rag-mvp/quickstart.md)

变更需求、数据、接口或配置时，先更新对应权威文档，再同步受影响任务；不要在多个文档维护同一份规则。

## 实施约束

- 严格按 Backend-First：完成并验证后端业务逻辑与冻结契约后，才能开始前端渲染。
- 后端使用 Python 3.12 与 `uv`；前端使用 Node.js 22 LTS、pnpm 与根目录唯一的 `pnpm-lock.yaml`。
- 实现目录与质量命令以 `plan.md`、`tasks.md` 和 `quickstart.md` 为准；代码与测试必须随任务同步交付。
- 不得绕过服务端授权、统一仓储、任务状态真相源或模型出口网关；具体边界由项目宪章和上述权威文档定义。
