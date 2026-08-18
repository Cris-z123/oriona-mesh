# OrionaMesh

OrionaMesh 是一个面向个人与轻量团队的开源 RAG 应用：用户将私有文本资料构建为知识库，并在连续对话中获得可定位、可追溯的回答。

本文只定义产品定位、MVP 范围和架构意图。进入开发后，具体需求、数据模型、API 契约、运行配置与实施任务必须分别以 `specs/001-orionamesh-rag-mvp/` 下的权威文档为准，避免在本文件维护第二份实现细节。

## 产品设计

### 产品定位

- 项目形态：面向终端用户的开源应用，不是通用基础设施库。
- 核心场景：用户上传 PDF、DOCX、Markdown、TXT 等文本资料，构建用户级隔离的知识库，并据此连续问答。
- 核心价值：用户级数据隔离、异步可诊断的资料处理、可追溯的检索证据，以及可复查的历史对话。

### MVP 范围

MVP 提供：

- 注册、登录、刷新会话、登出和个人资料维护；
- 私有知识库的创建、查看、修改和删除；
- PDF、DOCX、Markdown、TXT 的批量上传、异步处理、状态查看、失败诊断和删除；
- 绑定知识库的连续对话、SSE 流式回答与来源引用；
- 分级请求限流、模型出口安全、结构化日志、Docker Compose 部署与 CI 校验。

MVP 不提供：

- SSO、忘记密码或任何密码重置流程；
- 纯聊天对话；
- 面向用户的资料重新处理、源文件替换或版本切换；
- 默认启用的 Reranker、中文全文检索扩展或分布式对象存储。

资料版本字段、派生数据边界与清理结构会作为后续扩展骨架保留；重处理与替换 API、默认启用 Reranker 和检索质量调优属于 Phase 2 范围，不得提前作为 MVP 交付承诺。

## 架构意图

### 前后端职责

系统采用前后端分离架构：Next.js 前端仅负责界面、交互和后端授权数据的呈现；FastAPI 后端负责认证授权、业务规则、数据访问、异步编排和敏感信息处理。前后端通过版本化 REST API 协作，SSE 仅用于回答的单向流式传输，不能替代资源操作契约。

路由层负责协议转换和输入输出，服务层负责业务编排，仓储层负责持久化，worker 负责后台执行。客户端不得直接访问数据库、任务队列、资料存储或模型供应商；各层不得绕过其职责边界。

### 资料处理

资料采用异步流水线：

```text
upload → parse → chunk → embed → finalize → cleanup
```

- 上传只完成整批同步校验、持久化资料和初始任务，并尽快返回；处理不阻塞上传接口。
- `embed` 可按批次将向量化片段直接写入正式 `chunks`；在 `finalize` 完成校验并将资料发布为 `completed` 前，这些片段不可被业务读取或检索。
- 数据库中的资料、任务和尝试记录是处理状态的唯一业务真相源。Celery 与 Redis 只承担投递、执行和临时基础设施职责。
- worker 写入解析结果、草稿、正式片段或阶段结果时必须接受数据库 fencing 约束；删除提交后的陈旧 worker 不得继续写入。
- 任一处理分支必须收敛到明确结果。恢复扫描器负责接管失联任务、上传协调和失联的流式消息，不能让用户长期看到无法解释的处理中状态。

资料的完整状态机、任务与 attempt 语义、事务边界、删除接管和 fencing 规则由 [数据模型](../specs/001-orionamesh-rag-mvp/data-model.md) 唯一维护。

### 检索与可信回答

检索以会话绑定且经过授权的 `user_id` 与 `knowledge_base_id` 为唯一入口。向量和关键词两条召回路径均必须经统一 `ChunkRepository`，并强制按当前用户、知识库、当前文档版本和已完成状态过滤；不得在路由、服务或 worker 中直读 `chunks`。

检索流程为：查询改写、向量与关键词双路召回、各路证据门槛过滤、RRF 融合、可选重排、Context Pack、流式回答和引用持久化。无完成资料或融合后无证据时，系统必须明确拒答，且不得调用回答生成模型或伪装为知识库结论。

MVP 保持 Top-K、RRF 与 Context Pack 的固定默认策略；向量和关键词证据门槛允许按部署配置覆盖。Reranker 为可选内部适配器，未配置或失败时直接使用 RRF 结果。具体默认值、配置变量和验证规则以 [Quickstart](../specs/001-orionamesh-rag-mvp/quickstart.md) 为准。

### 删除、引用与版本

用户删除资料后，资料必须立即从普通列表、详情和检索中隐藏；后台 `delete_cleanup` 在安全接管运行任务后清理原始文件和派生数据。删除后的历史回答保留不可恢复的来源快照，供用户核验，不得由快照重新暴露原始资料。

资料与知识库删除、失败墓碑、重试删除、引用快照和版本可见性必须遵守 [数据模型](../specs/001-orionamesh-rag-mvp/data-model.md) 与 [OpenAPI 契约](../specs/001-orionamesh-rag-mvp/contracts/openapi.yaml)；不得在客户端自行推导删除或重试规则。

### 模型出口与数据安全

所有外部 Embedding、Query Rewrite、Reranker 和回答生成调用必须经过后端内部模型出口网关。网关负责供应商路由、凭证注入、发送前脱敏、超时、重试、稳定错误分类和白名单元数据审计；业务适配器只在网关最终失败后执行领域降级。

网关默认拒绝无法可靠脱敏的请求。日志、异常和审计记录不得包含提示词、问题、资料片段、文件名、请求或响应正文、请求头、密码、令牌和凭证。供应商与模型通过部署配置调整，不得在业务代码中写死。调用信封、允许外发内容、脱敏及审计字段由 [模型出口内部契约](../specs/001-orionamesh-rag-mvp/contracts/model-egress.md) 唯一维护。

### 存储、限流与部署

- MVP 使用挂载至容器内 `/data/orionamesh` 的本地持久卷；数据库只保存相对对象键。存储访问必须与后端无关，以便后续迁移对象存储。
- PostgreSQL（含 `pgvector` 与 `pg_trgm`）保存领域数据；Redis 只保存队列和短生命周期的限流计数，不得成为业务状态真相源。
- 请求限流在服务端实施：认证流量按可信来源 IP 与不可逆账号摘要限制，上传和问答按当前用户限制。原始邮箱、令牌、完整转发链和请求正文不得进入 Redis、日志或指标。
- 部署目标是 Linux 容器上的 Docker Compose 单机实例；GitHub Actions 在正式 tag 构建、扫描并发布
  前端/后端 `linux/amd64` 镜像归档，服务器校验后导入运行，既不访问 GHCR，也不在服务器构建应用镜像。

完整环境变量、依赖安装、迁移、就绪检查、Compose、质量工具和验证步骤由 [Quickstart](../specs/001-orionamesh-rag-mvp/quickstart.md) 唯一维护。

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | Next.js、React、TypeScript、Tailwind CSS、shadcn/ui、Pino、pnpm |
| 后端 | Python 3.12、FastAPI、Pydantic、SQLAlchemy、Alembic、LangChain、Celery、Redis、PostgreSQL、pgvector、pg_trgm、JWT、structlog、uv |
| 部署 | Docker Compose、GitHub Actions、GitHub Releases |

## 开发期文档边界

| 文档 | 唯一职责 |
|---|---|
| [.specify/memory/constitution.md](../.specify/memory/constitution.md) | 不可协商的项目原则与质量边界 |
| [spec.md](../specs/001-orionamesh-rag-mvp/spec.md) | 用户需求、范围、验收场景和成功标准 |
| [plan.md](../specs/001-orionamesh-rag-mvp/plan.md) | 实现架构、模块边界、实施顺序与设计阶段结论 |
| [research.md](../specs/001-orionamesh-rag-mvp/research.md) | 已选方案的决策理由与被放弃备选方案 |
| [data-model.md](../specs/001-orionamesh-rag-mvp/data-model.md) | 领域字段、关系、状态机、事务、不变量和数据边界 |
| [openapi.yaml](../specs/001-orionamesh-rag-mvp/contracts/openapi.yaml) | 公开 REST/SSE 路径、请求、响应、状态码与错误码契约 |
| [model-egress.md](../specs/001-orionamesh-rag-mvp/contracts/model-egress.md) | 内部模型出口调用、脱敏与审计契约 |
| [quickstart.md](../specs/001-orionamesh-rag-mvp/quickstart.md) | 环境配置、默认值、部署与验证步骤 |
| [tasks.md](../specs/001-orionamesh-rag-mvp/tasks.md) | 按依赖排序的实施与验证任务；不重新定义需求或契约 |

发生冲突时，先遵循项目宪章；同一领域内再以本表指定的唯一职责文档为准。对需求、数据模型、接口或配置的修改必须同步更新其唯一权威文档及受影响的任务，禁止在多个文档复制粘贴后分别维护。
